#  Copyright 2015-2016 Palo Alto Networks, Inc
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
This module implements master and slave hub classes for MineMeld engine
management bus.

Management bus master sends commands to all managemnt bus slaves by
posting a message to a specific topic (MGMTBUS_PREFIX+'bus').
Slaves subscribe to the topic, and when a command is received they
reply back to the master by sending the answer to the queue
MGMTBUS_PREFIX+'master'. Slaves connections are multiplexed via
slave hub class.

Management bus is used to control the MineMeld engine graph and to
periodically retrieve metrics from all the nodes.
"""

from __future__ import absolute_import

import logging
import uuid
import collections
import time

import gevent
import gevent.event
import gevent.lock

import redis
import ujson

import minemeld.comm
import minemeld.ft

from .collectd import CollectdClient

LOG = logging.getLogger(__name__)

MGMTBUS_PREFIX = "mbus:"
MGMTBUS_TOPIC = MGMTBUS_PREFIX+'bus'
MGMTBUS_MASTER = MGMTBUS_PREFIX+'master'
MGMTBUS_LOG_TOPIC = MGMTBUS_PREFIX+'log'
MGMTBUS_STATUS_TOPIC = MGMTBUS_PREFIX+'status'


class MgmtbusMaster(object):
    """MineMeld engine management bus master

    Args:
        ftlist (list): list of nodes
        config (dict): config
        comm_class (string): communication backend to be used
        comm_config (dict): config for the communication backend
    """
    def __init__(self, ftlist, config, comm_class, comm_config):
        super(MgmtbusMaster, self).__init__()

        self.ftlist = ftlist
        self.config = config
        self.comm_config = comm_config
        self.comm_class = comm_class
        self.graph_status = None

        self._start_timestamp = int(time.time())*1000
        self._status_lock = gevent.lock.Semaphore()
        self.status_glet = None
        self._status = {}

        self.SR = redis.StrictRedis.from_url(
            self.config.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
        )

        self.comm = minemeld.comm.factory(self.comm_class, self.comm_config)
        self._out_channel = self.comm.request_pub_channel(MGMTBUS_TOPIC)
        self.comm.request_rpc_server_channel(
            MGMTBUS_PREFIX+'master',
            self,
            allowed_methods=['rpc_status'],
            method_prefix='rpc_'
        )
        self._rpc_client = self.comm.request_rpc_fanout_client_channel(
            MGMTBUS_TOPIC
        )
        self.comm.request_sub_channel(
            MGMTBUS_STATUS_TOPIC,
            self,
            allowed_methods=['status'],
            name=MGMTBUS_STATUS_TOPIC+':master',
            max_length=100
        )

    def rpc_status(self):
        """Returns collected status via RPC
        """
        return self._status

    def _send_cmd(self, command, params=None, and_discard=False):
        """Sends command to slaves over mgmt bus.

        Args:
            command (str): command
            params (dict): params of the command
            and_discard (bool): discard answer, don't wait

        Returns:
            returns a gevent.event.AsyncResult that is signaled
            when all the answers are collected
        """
        if params is None:
            params = {}

        return self._rpc_client.send_rpc(
            command,
            params=params,
            and_discard=and_discard,
            num_results=len(self.ftlist)
        )

    def init_graph(self, config):
        """Initalizes graph by sending startup messages.

        Args:
            config (MineMeldConfig): config
        """
        revt = self._send_cmd('state_info')
        success = revt.wait(timeout=60)
        if success is None:
            LOG.critical('timeout in state_info, bailing out')
            raise RuntimeError('timeout in state_info')
        result = revt.get(block=False)

        if result['errors'] > 0:
            LOG.critical('errors reported from nodes in init_graph')
            raise RuntimeError('errors reported from nodes in init_graph')

        LOG.info('state: {}'.format(result['answers']))

        if len(config.changes) != 0:
            command = 'rebuild'
            LOG.info("new config: sending {}".format(command))
            self._send_cmd(command, and_discard=True)
            self.graph_status = 'INIT'
            return

        checkpoints = set([a.get('checkpoint', None)
                           for a in result['answers'].values()])
        if len(checkpoints) == 1:
            ccheckpoint = next(iter(checkpoints))
            if ccheckpoint is not None:
                LOG.info('all nodes at the same checkpoint (%s) '
                         ' sending initialize', ccheckpoint)
                self._send_cmd('initialize', and_discard=True)
                self.graph_status = 'INIT'
                return

        source_chkps = set([a.get('checkpoint', None)
                            for a in result['answers'].values()
                            if a['is_source']])
        if len(source_chkps) == 1:
            ccheckpoint = next(iter(source_chkps))
            if ccheckpoint is not None:
                LOG.info('all source nodes at the same checkpoint (%s) '
                         ' sending rebuild', ccheckpoint)
                self._send_cmd('rebuild', and_discard=True)
                self.graph_status = 'INIT'
                return

        LOG.info("sending reset")
        self._send_cmd('reset', and_discard=True)
        self.graph_status = 'INIT'

    def checkpoint_graph(self, max_tries=12):
        """Checkpoints the graph.

        Args:
            max_tries (int): number of minutes before giving up
        """
        LOG.info('checkpoint_graph called, checking current state')

        if self.graph_status != 'INIT':
            LOG.info('graph status {}, checkpoint_graph ignored'.format(self.graph_status))
            return

        while True:
            revt = self._send_cmd('state_info')
            success = revt.wait(timeout=30)
            if success is None:
                LOG.error('timeout in state_info')
                gevent.sleep(60)
                continue

            result = revt.get(block=False)
            if result['errors'] > 0:
                LOG.critical('errors reported from nodes in ' +
                             'checkpoint_graph: %s',
                             result['errors'])
                gevent.sleep(60)
                continue

            all_started = True
            for answer in result['answers'].values():
                if answer.get('state', None) != minemeld.ft.ft_states.STARTED:
                    all_started = False
                    break
            if not all_started:
                LOG.error('some nodes not started yet, waiting')
                gevent.sleep(60)
                continue

            break

        chkp = str(uuid.uuid4())

        revt = self._send_cmd('checkpoint', params={'value': chkp})
        success = revt.wait(timeout=60)
        if success is None:
            LOG.error('Timeout waiting for answers to checkpoint')
            return

        ntries = 0
        while ntries < max_tries:
            revt = self._send_cmd('state_info')
            success = revt.wait(timeout=60)
            if success is None:
                LOG.error("Error retrieving nodes states after checkpoint")
                gevent.sleep(30)
                continue

            result = revt.get(block=False)

            cgraphok = True
            for answer in result['answers'].values():
                cgraphok &= (answer['checkpoint'] == chkp)
            if cgraphok:
                LOG.info('checkpoint graph - all good')
                break

            gevent.sleep(10)
            ntries += 1

        if ntries == max_tries:
            LOG.error('checkpoint_graph: nodes still not in '
                      'checkpoint state after max_tries')

        self.graph_status = 'CHECKPOINT'

    def _send_collectd_metrics(self, answers, interval):
        """Send collected metrics from nodes to collectd.

        Args:
            answers (list): list of metrics
            interval (int): collection interval
        """
        collectd_socket = self.config.get(
            'COLLECTD_SOCKET',
            '/var/run/collectd.sock'
        )

        cc = CollectdClient(collectd_socket)

        gstats = collections.defaultdict(lambda: 0)

        for source, a in answers.iteritems():
            ntype = 'processors'
            if len(a.get('inputs', [])) == 0:
                ntype = 'miners'
            elif not a.get('output', False):
                ntype = 'outputs'

            stats = a.get('statistics', {})
            length = a.get('length', None)

            _, _, source = source.split(':', 2)

            for m, v in stats.iteritems():
                gstats[ntype+'.'+m] += v
                cc.putval(source+'.'+m, v,
                          interval=interval,
                          type_='minemeld_delta')

            if length is not None:
                gstats['length'] += length
                gstats[ntype+'.length'] += length
                cc.putval(
                    source+'.length',
                    length,
                    type_='minemeld_counter',
                    interval=interval
                )

        for gs, v in gstats.iteritems():
            type_ = 'minemeld_delta'
            if gs.endswith('length'):
                type_ = 'minemeld_counter'

            cc.putval('minemeld.'+gs, v, type_=type_, interval=interval)

    def _merge_status(self, nodename, status):
        currstatus = self._status.get(nodename, None)
        if currstatus is not None:
            if currstatus.get('clock', -1) > status.get('clock', -2):
                LOG.error('old clock: {} > {} - dropped'.format(
                    currstatus.get('clock', -1),
                    status.get('clock', -2)
                ))
                return

        self._status[nodename] = status

        try:
            source = nodename.split(':', 2)[2]
            self.SR.publish(
                'mm-engine-status.'+source,
                ujson.dumps({
                    'source': source,
                    'timestamp': int(time.time())*1000,
                    'status': status
                })
            )

        except:
            LOG.exception('Error publishing status')

    def _status_loop(self):
        """Greenlet that periodically retrieves metrics from nodes and sends
        them to collected.
        """
        loop_interval = self.config.get('STATUS_INTERVAL', '60')
        try:
            loop_interval = int(loop_interval)
        except ValueError:
            LOG.error('invalid STATUS_INTERVAL settings, '
                      'reverting to default')
            loop_interval = 60

        while True:
            revt = self._send_cmd('status')
            success = revt.wait(timeout=30)
            if success is None:
                LOG.error('timeout in waiting for status updates from nodes')
            else:
                result = revt.get(block=False)

                with self._status_lock:
                    for nodename, nodestatus in result['answers'].iteritems():
                        self._merge_status(nodename, nodestatus)

                try:
                    self._send_collectd_metrics(
                        result['answers'],
                        loop_interval
                    )

                except:
                    LOG.exception('Exception in _status_loop')

            gevent.sleep(loop_interval)

    def status(self, timestamp, **kwargs):
        source = kwargs.get('source', None)
        if source is None:
            LOG.error('no source in status report - dropped')
            return

        status = kwargs.get('status', None)
        if status is None:
            LOG.error('no status in status report - dropped')
            return

        if self._status_lock.locked():
            return

        with self._status_lock:
            if timestamp < self._start_timestamp:
                return

            self._merge_status('mbus:slave:'+source, status)

    def start_status_monitor(self):
        """Starts status monitor greenlet.
        """
        if self.status_glet is not None:
            LOG.error('double call to start_status')
            return

        self.status_glet = gevent.spawn(self._status_loop)

    def stop_status_monitor(self):
        """Stops status monitor greenlet.
        """
        if self.status_glet is None:
            return
        self.status_glet.kill()
        self.status_glet = None

    def start(self):
        self.comm.start()

    def stop(self):
        self.comm.stop()


class MgmtbusSlaveHub(object):
    """Hub MineMeld engine management bus slaves. Each chassis
        has an instance of this class, and each node in the chassis
        request a channel to the management bus via this instance.

    Args:
        config (dict): config
        comm_class (string): communication backend to be used
        comm_config (dict): config for the communication backend
    """

    def __init__(self, config, comm_class, comm_config):
        self.config = config
        self.comm_config = comm_config
        self.comm_class = comm_class

        self.comm = minemeld.comm.factory(self.comm_class, self.comm_config)

    def request_log_channel(self):
        LOG.debug("Adding log channel")
        return self.comm.request_pub_channel(
            MGMTBUS_LOG_TOPIC
        )

    def request_status_channel(self):
        LOG.debug("Adding status channel")
        return self.comm.request_pub_channel(
            MGMTBUS_STATUS_TOPIC
        )

    def request_channel(self, node):
        self.comm.request_rpc_server_channel(
            MGMTBUS_PREFIX+'slave:'+node.name,
            node,
            allowed_methods=[
                'mgmtbus_state_info',
                'mgmtbus_initialize',
                'mgmtbus_rebuild',
                'mgmtbus_reset',
                'mgmtbus_status',
                'mgmtbus_checkpoint'
            ],
            method_prefix='mgmtbus_',
            fanout=MGMTBUS_TOPIC
        )

    def add_failure_listener(self, f):
        self.comm.add_failure_listener(f)

    def start(self):
        LOG.debug('mgmtbus start called')
        self.comm.start()

    def stop(self):
        self.comm.stop()


def master_factory(config, comm_class, comm_config, fts):
    """Factory of management bus master instances

    Args:
        config (dict): management bus master config
        comm_class (string): communication backend.
            Unused, AMQP is always used
        comm_config (dict): config of the communication backend
        fts (list): list of nodes

    Returns:
        Instance of minemeld.mgmtbus.MgmtbusMaster class
    """
    _ = comm_class  # noqa

    return MgmtbusMaster(
        fts,
        config,
        'AMQP',
        comm_config
    )


def slave_hub_factory(config, comm_class, comm_config):
    """Factory of management bus slave hub instances

    Args:
        config (dict): management bus master config
        comm_class (string): communication backend.
            Unused, AMQP is always used
        comm_config (dict): config of the communication backend.

    Returns:
        Instance of minemeld.mgmtbus.MgmtbusSlaveHub class
    """
    _ = comm_class  # noqa

    return MgmtbusSlaveHub(
        config,
        'AMQP',
        comm_config
    )
