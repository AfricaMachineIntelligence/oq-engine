#  -*- coding: utf-8 -*-
#  vim: tabstop=4 shiftwidth=4 softtabstop=4

#  Copyright (C) 2016-2017 GEM Foundation

#  OpenQuake is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  OpenQuake is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU Affero General Public License
#  along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import sys
import time
import socket
import sqlite3
import os.path
import logging
import subprocess
from multiprocessing.connection import Listener
from concurrent.futures import ThreadPoolExecutor
from openquake.baselib import sap
from openquake.baselib.parallel import safely_call
from openquake.hazardlib import valid
from openquake.commonlib import config, logs
from openquake.server.db import actions
from openquake.server import dbapi
from openquake.server import __file__ as server_path
from openquake.server.settings import DATABASE

zmq = os.environ.get('OQ_DISTRIBUTE') == 'zmq'
if zmq:
    import zmq

# using a ThreadPool because SQLite3 isn't fork-safe on macOS Sierra
# ref: https://bugs.python.org/issue27126
executor = ThreadPoolExecutor(1)


def worker(begin_addr, end_addr):
    """
    A worker receiving commands, executing them and sending back replies
    """
    context = zmq.Context()
    receiver = context.socket(zmq.PULL)
    receiver.connect(begin_addr)
    sender = context.socket(zmq.PUSH)
    sender.connect(end_addr)
    while True:
        try:
            func, args = receiver.recv_pyobj()
        except KeyboardInterrupt:
            break
        if func == 'stop':
            break
        triple = safely_call(func, args)
        sender.send_pyobj(triple)

WORKER = "from openquake.server.dbserver import worker; worker('%s', '%s')"


class DbServer(object):
    """
    A server collecting the received commands into a queue
    """
    def __init__(self, db, address, authkey):
        self.db = db
        self.address = address
        host, port = address
        self.authkey = authkey
        if host == 'localhost':
            host = '127.0.0.1'
        self.begin_address = 'tcp://%s:%s' % (host, port + 1)
        self.end_address = 'tcp://%s:%s' % (host, port + 2)

    def __enter__(self):
        if zmq:
            # create the workers
            self.workers = 0
            for host, cores in config.get_host_cores():
                for core in range(cores):
                    args = [sys.executable, '-c', WORKER % (self.begin_address,
                                                            self.end_address)]
                    if host != '127.0.0.1':
                        args = ['ssh'] + args
                    logging.warn('Starting %s' % ' '.join(args))
                    subprocess.Popen(args)
                    self.workers += 1
        return self

    def __exit__(self, etype, exc, tb):
        if zmq:
            sender = zmq.Context().socket(zmq.PUSH)
            sender.bind(self.begin_address)
            for i in range(self.workers):
                sender.send_pyobj(('stop', i))
            sender.close()

    def loop(self):
        listener = Listener(self.address, backlog=5, authkey=self.authkey)
        logging.warn('DB server started with %s, listening on %s:%d...',
                     sys.executable, *self.address)
        try:
            while True:
                try:
                    conn = listener.accept()
                except KeyboardInterrupt:
                    break
                except:
                    # unauthenticated connection, for instance by a port
                    # scanner such as the one in manage.py
                    continue
                cmd_ = conn.recv()  # a tuple (name, arg1, ... argN)
                cmd, args = cmd_[0], cmd_[1:]
                logging.debug('Got ' + str(cmd_))
                if cmd == 'stop':
                    conn.send((None, None))
                    conn.close()
                    break
                func = getattr(actions, cmd)
                fut = executor.submit(safely_call, func, (self.db,) + args)

                def sendback(fut, conn=conn):
                    res, etype, _mon = fut.result()
                    if etype:
                        logging.error(res)
                    # send back the result and the exception class
                    conn.send((res, etype))
                    conn.close()
                fut.add_done_callback(sendback)
        finally:
            listener.close()


def different_paths(path1, path2):
    path1 = os.path.realpath(path1)  # expand symlinks
    path2 = os.path.realpath(path2)  # expand symlinks
    # don't care about the extension (it may be .py or .pyc)
    return os.path.splitext(path1)[0] != os.path.splitext(path2)[0]


def get_status(address=None):
    """
    Check if the DbServer is up.

    :param address: pair (hostname, port)
    :returns: 'running' or 'not-running'
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        err = sock.connect_ex(address or config.DBS_ADDRESS)
    finally:
        sock.close()
    return 'not-running' if err else 'running'


def check_foreign():
    """
    Check if we the DbServer is the right one
    """
    if not config.flag_set('dbserver', 'multi_user'):
        remote_server_path = logs.dbcmd('get_path')
        if different_paths(server_path, remote_server_path):
            return('You are trying to contact a DbServer from another'
                   ' instance (got %s, expected %s)\n'
                   'Check the configuration or stop the foreign'
                   ' DbServer instance') % (remote_server_path, server_path)


def ensure_on():
    """
    Start the DbServer if it is off
    """
    if get_status() == 'not-running':
        if valid.boolean(config.get('dbserver', 'multi_user')):
            sys.exit('Please start the DbServer: '
                     'see the documentation for details')
        # otherwise start the DbServer automatically
        subprocess.Popen([sys.executable, '-m', 'openquake.server.dbserver',
                          '-l', 'INFO'])

        # wait for the dbserver to start
        waiting_seconds = 10
        while get_status() == 'not-running':
            if waiting_seconds == 0:
                sys.exit('The DbServer cannot be started after 10 seconds. '
                         'Please check the configuration')
            time.sleep(1)
            waiting_seconds -= 1


@sap.Script
def run_server(dbhostport=None, dbpath=None, logfile=DATABASE['LOG'],
               loglevel='WARN'):
    """
    Run the DbServer on the given database file and port. If not given,
    use the settings in openquake.cfg.
    """
    if dbhostport:  # assume a string of the form "dbhost:port"
        dbhost, port = dbhostport.split(':')
        addr = (dbhost, int(port))
        DATABASE['PORT'] = int(port)
    else:
        addr = config.DBS_ADDRESS

    if dbpath:
        DATABASE['NAME'] = dbpath

    # create the db directory if needed
    dirname = os.path.dirname(DATABASE['NAME'])
    if not os.path.exists(dirname):
        os.makedirs(dirname)

    # create and upgrade the db if needed
    db = dbapi.Db(sqlite3.connect, DATABASE['NAME'], isolation_level=None,
                  detect_types=sqlite3.PARSE_DECLTYPES)
    db('PRAGMA foreign_keys = ON')  # honor ON DELETE CASCADE
    actions.upgrade_db(db)
    db.conn.close()

    # configure logging and start the server
    logging.basicConfig(level=getattr(logging, loglevel), filename=logfile)
    with DbServer(db, addr, config.DBS_AUTHKEY) as dbs:
        dbs.loop()

run_server.arg('dbhostport', 'dbhost:port')
run_server.arg('dbpath', 'dbpath')
run_server.arg('logfile', 'log file')
run_server.opt('loglevel', 'WARN or INFO')

if __name__ == '__main__':
    run_server.callfunc()
