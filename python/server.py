#! /usr/bin/env python

import SimpleHTTPServer
import SocketServer
import BaseHTTPServer
import cgi
import logging
import urllib
import sys, os
import time
import thread
import threading
import sys
from math import log, exp
from thread import start_new_thread
from exchanges import *
from utils import *
import config

_wrappers = { 'poloniex' : Poloniex, 'ccedk' : CCEDK, 'bitcoincoid' : BitcoinCoId, 'bter' : BTER }
for e in config._interest:
  _wrappers[e] = _wrappers[e]()
  for u in config._interest[e]:
    for s in ['bid', 'ask']:
      config._interest[e][u][s]['orders'] = []

try: os.makedirs('logs')
except: pass

dummylogger = logging.getLogger('null')
dummylogger.addHandler(logging.NullHandler())
dummylogger.propagate = False

logname = str(int(time.time()*100))
creditor = logging.getLogger("credits")
creditor.propagate = False
creditformat = logging.Formatter(fmt = '%(asctime)s: %(message)s', datefmt="%Y/%m/%d-%H:%M:%S")
ch = logging.FileHandler('logs/%s.credits' % logname)
ch.setFormatter(creditformat)
creditor.addHandler(ch)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler('logs/%s.log' % logname)
fh.setLevel(logging.DEBUG)
sh = logging.StreamHandler()
sh.setLevel(logging.INFO)

formatter = logging.Formatter(fmt = '%(asctime)s %(levelname)s: %(message)s', datefmt="%Y/%m/%d-%H:%M:%S")
fh.setFormatter(formatter)
sh.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(sh)
_liquidity = []
_active_users = 0

keys = {}
pricefeed = PriceFeed(30, logger)
lock = threading.Lock()

class NuRPC():
  def __init__(self, config, address, logger = None):
    self.logger = logger if logger else logging.getLogger('null')
    self.address = address
    self.rpc = None
    try:
      import jsonrpc
    except ImportError:
      self.logger.warning('NuRPC: jsonrpc library could not be imported')
    else:
      # rpc connection
      self.JSONRPCException = jsonrpc.JSONRPCException
      opts = dict(tuple(line.strip().replace(' ','').split('=')) for line in open(config).readlines() if len(line.split('=')) == 2)
      if not 'rpcuser' in opts.keys() or not 'rpcpassword' in opts.keys():
        self.logger.error("NuRPC: RPC parameters could not be read")
      else:
        try:
          self.rpc = jsonrpc.ServiceProxy("http://%s:%s@127.0.0.1:%s"%(
            opts['rpcuser'],opts['rpcpassword'], 14002))
          self.txfee = self.rpc.getinfo()['paytxfee']
        except:
          self.logger.error("NuRPC: RPC connection could not be established")
          self.rpc = None

  def pay(self, txout):
    try:
      self.rpc.sendmany("", txout)
      self.logger.info("successfully sent payout: %s", txout)
      return True
    except AttributeError:
      self.logger.error('NuRPC: client not initialized')
    except self.JSONRPCException as e:
      self.logger.error('NuRPC: unable to send payout: %s', e.error['message'])
    except:
      self.logger.error("NuRPC: unable to send payout (exception caught): %s", sys.exc_info()[1])
    return False

  def liquidity(self, bid, ask):
    try:
      self.rpc.liquidityinfo('B', bid, ask, self.address)
      print response
      self.logger.info("successfully sent liquidity: buy: %.8f sell: %.8f", bid, ask)
      return True
    except AttributeError:
      self.logger.error('NuRPC: client not initialized')
    except self.JSONRPCException as e:
      self.logger.error('NuRPC: unable to send liquidity: %s', e.error['message'])
    except:
      self.logger.error("NuRPC: unable to send liquidity (exception caught): %s", sys.exc_info()[1])
    return False

class User(threading.Thread):
  def __init__(self, key, address, unit, exchange, pricefeed, sampling, tolerance, logger = None):
    threading.Thread.__init__(self)
    self.key = key
    self.active = False
    self.address = address
    self.balance = 0.0
    self.pricefeed = pricefeed
    self.unit = unit
    self.exchange = exchange
    self.tolerance = tolerance
    self.sampling = sampling
    self.last_error = ""
    self.cost = { 'ask' : config._interest[repr(exchange)][unit]['bid']['rate'], 'bid' : config._interest[repr(exchange)][unit]['ask']['rate'] }
    self.rate = { 'ask' : config._interest[repr(exchange)][unit]['bid']['rate'], 'bid' : config._interest[repr(exchange)][unit]['ask']['rate'] }
    self.liquidity = { 'ask' : [[]] * sampling, 'bid' : [[]] * sampling }
    self.lock = threading.Lock()
    self.trigger = threading.Lock()
    self.trigger.acquire()
    self.response = ['m'] * sampling
    self.logger = logger if logger else logging.getLogger('null')
    self.requests = []
    self.daemon = True

  def set(self, request, bid, ask, sign):
    if len(self.requests) < 10: # don't accept more requests to avoid simple spamming
      self.lock.acquire()
      if len(self.requests) < 10: # double check to allow lock acquire above
        self.requests.append(({ p : v[0] for p,v in request.items() }, sign, { 'bid': bid, 'ask': ask }))
      self.lock.release()
    self.active = True

  def run(self):
    while True:
      self.trigger.acquire()
      self.lock.acquire()
      if self.active:
        del self.response[0]
        res = 'm'
        if self.requests:
          for rid, request in enumerate(self.requests):
            try:
              orders = self.exchange.validate_request(self.key, self.unit, request[0], request[1])
            except:
              orders = { 'error' : 'exception caught: %s' % sys.exc_info()[1]}
            if not 'error' in orders:
              valid = { 'bid': [], 'ask' : [] }
              price = self.pricefeed.price(self.unit)
              for order in orders:
                deviation = 1.0 - min(order['price'], price) / max(order['price'], price)
                if deviation <= self.tolerance:
                  valid[order['type']].append([order['id'], order['amount'], request[2][order['type']]])
                else:
                  self.last_error = 'unable to validate request: order of deviates too much from current price'
              for side in [ 'bid', 'ask' ]:
                del self.liquidity[side][0]
                self.liquidity[side].append(valid[side])
              if self.last_error != "" and len(valid['bid'] + valid['ask']) == 0:
                res = 'r'
                self.logger.debug("unable to validate request %d/%d for user %s at exchange %s on unit %s: orders of deviate too much from current price" % (rid + 1, len(self.requests), self.key, repr(self.exchange), self.unit))
              else:
                res = 'a'
                break
            else:
              res = 'r'
              self.last_error = "unable to validate request: " + orders['error']
              if rid + 1 == len(self.requests):
                self.logger.warning("unable to validate request %d/%d for user %s at exchange %s on unit %s: %s",
                  rid + 1, len(self.requests), self.key, repr(self.exchange), self.unit, orders['error'])
              for side in [ 'bid', 'ask' ]:
                del self.liquidity[side][0]
                self.liquidity[side].append([])
        else:
          self.last_error = "no request received"
          logger.debug("no request received for user %s at exchange %s on unit %s" % (self.key, repr(self.exchange), self.unit))
          for side in [ 'bid', 'ask' ]:
            del self.liquidity[side][0]
            self.liquidity[side].append([])
          self.active = False
        self.response.append(res)
        self.requests = []
      else:
        self.last_error = "no request received"
        for side in [ 'bid', 'ask' ]:
          del self.liquidity[side][0]
          self.liquidity[side].append([])
      self.lock.release()

  def validate(self):
    try: self.trigger.release()
    except thread.error: pass # user did not finish last request in time

  def finish(self):
    if self.active:
      try:
        self.lock.acquire()
        self.lock.release()
      except KeyboardInterrupt:
        raise

def response(errcode = 0, message = 'success'):
  return { 'code' : errcode, 'message' : message }

def register(params):
  ret = response()
  if set(params.keys()) == set(['address', 'key', 'name']):
    user = params['key'][0]
    name = params['name'][0]
    address = params['address'][0]
    if address[0] == 'B': # this is certainly not a proper check
      if name in _wrappers:
        if not user in keys:
          lock.acquire()
          keys[user] = {}
          for unit in config._interest[name]:
            keys[user][unit] = User(user, address, unit, _wrappers[name], pricefeed, config._sampling, config._tolerance, logger)
            keys[user][unit].start()
          lock.release()
          logger.info("new user %s on %s: %s" % (user, name, address))
        elif keys[user].values()[0].address != address:
          ret = response(9, "user already exists with different address: %s" % user)
      else:
        ret = response(8, "unknown exchange requested: %s" % name)
    else:
      ret = response(7, "invalid payout address: %s" % address)
  else:
    ret = response(6, "invalid registration data received: %s" % str(params))
  return ret

def liquidity(params):
  ret = response()
  if set(params.keys() + ['user', 'sign', 'unit', 'ask', 'bid']) == set(params.keys()):
    user = params.pop('user')[0]
    sign = params.pop('sign')[0]
    unit = params.pop('unit')[0]
    try:
      bid = float(params.pop('bid')[0])
      ask = float(params.pop('ask')[0])
      if user in keys:
        if unit in keys[user]:
          keys[user][unit].set(params, bid, ask, sign)
        else:
          ret = response(12, "unit for user %s not found: %s" % (user, unit))
      else:
          ret = response(11, "user not found: %s" % user)
    except ValueError:
      ret = response(10, "invalid cost information received: %s" % str(params))
  else:
    ret = response(9, "invalid liquidity data received: %s" % str(params))
  return ret

def poolstats():
  return { 'liquidity' : ([ (0,0) ] + _liquidity)[-1], 'sampling' : config._sampling, 'users' : _active_users }

def userstats(user):
  res = { 'balance' : 0.0, 'efficiency' : 0.0, 'rejects': 0, 'missing' : 0 }
  res['units'] = {}
  for unit in keys[user]:
    if keys[user][unit].active:
      bid = [[]] + [ x for x in keys[user][unit].liquidity['bid'] if x ]
      ask = [[]] + [ x for x in keys[user][unit].liquidity['ask'] if x ]
      missing = keys[user][unit].response.count('m')
      rejects = keys[user][unit].response.count('r')
      res['balance'] += keys[user][unit].balance
      res['missing'] += missing
      res['rejects'] += rejects
      res['units'][unit] = { 'bid' : bid[-1],
                             'ask' : ask[-1],
                             'rate' : keys[user][unit].rate,
                             'rejects' : rejects,
                             'missing' : missing,
                             'last_error' :  keys[user][unit].last_error }
  if len(res['units']) > 0:
    res['efficiency'] = 1.0 - (res['rejects'] + res['missing']) / float(config._sampling * len(res['units']))
  return res

def calculate_interest(balance, amount, target, rate):
  return max(min(amount, target - balance) * rate, 0.0)
  #try: # this is not possible with python floating arithmetic
  #  return interest['rate'] * (amount - (log(exp(interest['target']) + exp(balance + amount)) - log(exp(interest['target']) + exp(balance))))
  #except OverflowError:
  #  logger.error("overflow error in interest calculation, balance: %.8f amount: %.8f", balance, amount)
  #  return 0.00001

def credit():
  for name in config._interest:
    for unit in config._interest[name]:
      users = [ k for k in keys if unit in keys[k] and repr(keys[k][unit].exchange) == name ]
      for user in users:
        keys[user][unit].rate['bid'] = 0.0
        keys[user][unit].rate['ask'] = 0.0
      for side in [ 'bid', 'ask' ]:
        config._interest[name][unit][side]['orders'] = []
        for sample in xrange(config._sampling):
          config._interest[name][unit][side]['orders'].append([])
          orders = []
          for user in users:
            orders += [ (user, order) for order in keys[user][unit].liquidity[side][sample] if order[2] <= config._interest[name][unit][side]['rate'] ]
          orders.sort(key = lambda x: (x[1][2], x[1][0]))
          balance = 0.0
          previd = -1
          mass = sum([orders[i][1][1] for i in xrange(len(orders)) if i == 0 or orders[i][1][0] != orders[i - 1][1][0]])
          residual = mass - config._interest[name][unit][side]['target']
          weight = { user : sum([o[1][1] for o in orders if o[0] == user]) for user in users }
          if residual > 0:
            for i in xrange(len(orders)):
              user, order = orders[i]
              if order[0] != previd:
                previd = order[0]
                if weight[user] > 0:
                  rate = order[2]
                  for j in xrange(i + 1, len(orders)):
                    if orders[j][1][2] > rate and orders[j][1][2] <= config._interest[name][unit][side]['rate']:
                      rate = orders[j][1][2]
                      break
                  if rate == order[2]:
                    rate = config._interest[name][unit][side]['rate']
                  amount = min(order[1], residual - balance)
                  if amount > 0:
                    payout = calculate_interest(balance, amount, residual, rate) / (config._sampling * 60 * 24)
                    keys[user][unit].balance += payout
                    orders[i][1][1] -= amount
                    balance += amount
                    keys[user][unit].rate[side] += 60 * 24 * payout / weight[user]
                    if payout > 0:
                      creditor.info("[%d/%d] %.8f %s %.8f %s %s %s %.8f %.2f",
                        sample + 1, config._sampling, payout, user, amount, side, name, unit, balance - amount, rate * 100)
                    config._interest[name][unit][side]['orders'][sample].append( { 'id': order[0], 'amount' : amount, 'cost' : config._sampling * 60 * 24 * payout / amount } )
                  if balance >= config._interest[name][unit][side]['target'] or balance >= residual: break
                else:
                  logger.warning('detected zero weight order for user %s: %s', user, str(order))
          rate = config._interest[name][unit][side]['rate']
          previd = -1
          for i in xrange(len(orders)):
            user, order = orders[i]
            if order[0] != previd and order[1] > 0:
              previd = order[0]
              if weight[user] > 0:
                amount = order[1] if order[1] < (config._interest[name][unit][side]['target'] - balance) else (config._interest[name][unit][side]['target'] - balance)
                payout = calculate_interest(balance, amount, config._interest[name][unit][side]['target'], rate) / (config._sampling * 60 * 24)
                keys[user][unit].balance += payout
                balance += amount
                keys[user][unit].rate[side] += 60 * 24 * payout / weight[user]
                if payout > 0:
                  creditor.info("[%d/%d] %.8f %s %.8f %s %s %s %.8f %.2f",
                    sample + 1, config._sampling, payout, user, order[1], side, name, unit, balance - order[1], rate * 100)
                if amount != order[1]:
                  if amount > 0:
                    config._interest[name][unit][side]['orders'][sample].append( { 'id': order[0], 'amount' : amount, 'cost' : config._sampling * 60 * 24 * payout / (amount if amount else 1) } )
                  config._interest[name][unit][side]['orders'][sample].append( { 'id': order[0], 'amount' : order[1] - amount, 'cost' : 0.0 } )
                else:
                  config._interest[name][unit][side]['orders'][sample].append( { 'id': order[0], 'amount' : order[1], 'cost' : config._sampling * 60 * 24 * payout / order[1] } )
              else:
                logger.warning('detected zero weight order for user %s: %s', user, str(order))

def pay(nud):
  txout = {}
  lock.acquire()
  for user in keys:
    for unit in keys[user]:
      if not keys[user][unit].address in txout:
        txout[keys[user][unit].address] = 0.0
      txout[keys[user][unit].address] += keys[user][unit].balance
  lock.release()
  txfee = 0.01 if not nud.rpc else nud.txfee
  txout = {k : v - nud.txfee for k,v in txout.items() if v - txfee > config._minpayout}
  if txout:
    payed = False
    if config._autopayout:
      payed = nud.pay(txout)
    try:
      filename = 'logs/%d.credit' % time.time()
      out = open(filename, 'w')
      out.write(json.dumps(txout))
      out.close()
      if not payed:
        logger.info("successfully stored payout to %s: %s", filename, txout)
      lock.acquire()
      for user in keys:
        for unit in keys[user]:
          if keys[user][unit].address in txout:
            keys[user][unit].balance = 0.0
      lock.release()
    except: logger.error("failed to store payout to %s: %s", filename, txout)
  else:
    logger.warning("not processing payouts because no valid balances were detected.")

def submit(nud):
  curliquidity = [0,0]
  lock.acquire()
  for user in keys:
    for unit in keys[user]:
      for s in xrange(config._sampling):
        curliquidity[0] += sum([ order[1] for order in keys[user][unit].liquidity['bid'][-(s+1)] ])
        curliquidity[1] += sum([ order[1] for order in keys[user][unit].liquidity['ask'][-(s+1)] ])
  lock.release()
  curliquidity = [ curliquidity[0] / float(config._sampling), curliquidity[1] / float(config._sampling) ]
  _liquidity.append(curliquidity)
  nud.liquidity(curliquidity[0], curliquidity[1])

class RequestHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
  def do_POST(self):
    if len(self.path) == 0:
      self.send_response(404)
      return
    self.path = self.path[1:]
    if self.path in ['register', 'liquidity']:
      ctype, pdict = cgi.parse_header(self.headers.getheader('content-type'))
      if ctype == 'application/x-www-form-urlencoded':
        length = int(self.headers.getheader('content-length'))
        params = cgi.parse_qs(self.rfile.read(length), keep_blank_values = 1)
        if self.path == 'liquidity':
          ret = liquidity(params)
        elif self.path == 'register':
          ret = register(params)
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.wfile.write("\n")
      self.wfile.write(json.dumps(ret))
      self.end_headers()

  def do_GET(self):
    if len(self.path) == 0:
      self.send_response(404)
      return
    method = self.path[1:]
    if 'loaderio' in method: # evil hack to support load tester (TODO)
      self.send_response(200)
      self.send_header('Content-Type', 'text/plain')
      self.wfile.write("\n")
      self.wfile.write(method.replace('/',''))
      self.end_headers()
    elif method in [ 'status', 'exchanges' ]:
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.wfile.write("\n")
      if method == 'status':
        self.wfile.write(json.dumps(poolstats()))
      elif method == 'exchanges':
        self.wfile.write(json.dumps(config._interest))
      self.end_headers()
    elif method in keys:
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.wfile.write("\n")
      self.wfile.write(json.dumps(userstats(method)))
      self.end_headers()
    elif '/' in method:
      root = method.split('/')[0]
      method = method.split('/')[1:]
      if root == 'price':
        price = { 'price' : pricefeed.price(method[0]) }
        if price['price']:
          self.send_response(200)
          self.send_header('Content-Type', 'application/json')
          self.wfile.write("\n")
          self.wfile.write(json.dumps(price))
          self.end_headers()
        else:
          self.send_response(404)
      elif root == 'info':
        if len(method) == 2 and method[0] in config._interest and method[1] in config._interest[method[0]]:
          self.send_response(200)
          self.send_header('Content-Type', 'application/json')
          self.wfile.write("\n")
          self.wfile.write(json.dumps(config._interest[method[0]][method[1]]))
          self.end_headers()
        else:
          self.send_response(404)
      else:
        self.send_response(404)
    else:
      self.send_response(404)

  def log_message(self, format, *args): pass

class ThreadingServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
  pass
#  def get_request(self):
#    self.socket.settimeout(self.timeout)
#    result = None
#    while result is None:
#      try:
#        result = self.socket.accept()
#      except socket.timeout:
#        pass
#    # Reset timeout on the new socket
#    result[0].settimeout(None)
#    return result

nud = NuRPC(config._nuconfig, config._grantaddress, logger)
if not nud.rpc:
  logger.critical('Connection to Nu daemon could not be established, liquidity will NOT be sent!')
  config._autopayout = False
httpd = ThreadingServer(("", config._port), RequestHandler)
sa = httpd.socket.getsockname()
logger.debug("Serving on %s port %d", sa[0], sa[1])
#httpd.timeout = 5
start_new_thread(httpd.serve_forever, ())

lastcredit = time.time()
lastpayout = time.time()
lastsubmit = time.time()

critical_message = ""

while True:
  try:
    curtime = time.time()

    # wait for validation round to end:
    lock.acquire()
    _active_users = 0
    for user in keys:
      active = False
      for unit in keys[user]:
        keys[user][unit].finish()
        active = active or keys[user][unit].active
      if active: _active_users += 1
    lock.release()

    # send liquidity
    if curtime - lastsubmit >= 60:
      submit(nud)
      lastsubmit = curtime

    # credit requests
    if curtime - lastcredit >= 60:
      credit()
      lastcredit = curtime

    # make payout
    if curtime - lastpayout >= 21600: #3600: #43200:
      pay(nud)
      lastpayout = curtime

    # start new validation round
    lock.acquire()
    for user in keys:
      for unit in keys[user]:
        keys[user][unit].last_error = critical_message
        keys[user][unit].validate()
    lock.release()

    time.sleep(max(float(60 / config._sampling) - time.time() + curtime, 0))
  except Exception as e:
    logger.error('exception caught in main loop: %s', sys.exc_info()[1])
    httpd.socket.close()
    raise
