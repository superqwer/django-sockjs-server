from collections import defaultdict
import json
import logging
from django.conf import settings
from django.utils.timezone import now
import pika
from pika.adapters.tornado_connection import TornadoConnection
from pika.exceptions import AMQPConnectionError
import time
from django_sockjs_server.lib.config import SockJSServerSettings
from django_sockjs_server.lib.redis_client import redis_client


class PikaClient(object):
    def __init__(self, io_loop):
        self.logger = logging.getLogger(__name__)
        self.logger.info('PikaClient: __init__')
        self.io_loop = io_loop

        self.connected = False
        self.connecting = False
        self.connection = None
        self.channel = None

        self.redis = redis_client
        self.event_listeners_count = 0
        self.event_listeners = set()
        self.subscrib_channel = dict()
        self.last_reconnect = now()
        self.uptime_start = now()



        self.config = SockJSServerSettings()

    def connect(self):
        if self.connecting:
            self.logger.info('django-sockjs-server(PikaClient): Already connecting to RabbitMQ')
            return

        self.logger.info('django-sockjs-server(PikaClient): Connecting to RabbitMQ')
        self.connecting = True

        cred = pika.PlainCredentials(self.config.rabbitmq_user, self.config.rabbitmq_password)
        param = pika.ConnectionParameters(
            host=self.config.rabbitmq_host,
            port=self.config.rabbitmq_port,
            virtual_host=self.config.rabbitmq_vhost,
            credentials=cred
        )

        try:
            self.connection = TornadoConnection(param,
                                                on_open_callback=self.on_connected)
            self.connection.add_on_close_callback(self.on_closed)
        except AMQPConnectionError:
            self.logger.info('django-sockjs-server(PikaClient): error connect, wait 5 sec')
            time.sleep(5)
            self.reconnect()

        self.last_reconnect = now()

    def on_connected(self, connection):
        self.logger.info('django-sockjs-server(PikaClient): connected to RabbitMQ')
        self.connected = True
        self.connection = connection
        self.connection.channel(self.on_channel_open)

    def on_channel_open(self, channel):
        self.logger.info('django-sockjs-server(PikaClient): Channel open, Declaring exchange')
        self.channel = channel
        self.channel.exchange_declare(exchange=self.config.rabbitmq_exchange_name,
                                      exchange_type=self.config.rabbitmq_exchange_type)
        self.channel.queue_declare(
            queue=self.config.rabbitmq_queue_name, 
            exclusive=False, 
            auto_delete=True, 
            callback=self.on_queue_declared
        )

    def on_queue_declared(self, frame):
        self.logger.info('django-sockjs-server(PikaClient): queue bind')
        self.queue = frame.method.queue
        self.channel.queue_bind(callback=None, exchange=self.config.rabbitmq_exchange_name, queue=frame.method.queue)
        self.channel.basic_consume(self.handle_delivery, queue=frame.method.queue, no_ack=True)

    def handle_delivery(self, channel, method, header, body):
        """Called when we receive a message from RabbitMQ"""
        self.notify_listeners(body)

    def on_closed(self, connection, error_code, error_message):
        self.logger.info('django-sockjs-server(PikaClient): rabbit connection closed, wait 5 seconds')
        connection.add_timeout(5, self.reconnect)

    def reconnect(self):
        self.connecting = False
        self.logger.info('django-sockjs-server(PikaClient): reconnect')
        self.connect()

    def notify_listeners(self, event_json):
        event_obj = json.loads(event_json)

        self.logger.debug('django-sockjs-server(PikaClient): send message %s ' % event_obj)
        try:
            channel = self.subscrib_channel[event_obj['uid']]
        except KeyError:
            self.redis.lrem(event_obj['room'], 0, json.dumps({'id': event_obj['uid'], 'host': event_obj['host']}))
        else:
            client = channel['conn']
            new_event_json = json.dumps({'data': event_obj['data']})
            client.broadcast([client], new_event_json)

    def add_event_listener(self, listener):
        self.event_listeners_count += 1
        self.event_listeners.add(listener)
        self.logger.debug('django-sockjs-server(PikaClient): listener %s added' % repr(listener))

    def remove_event_listener(self, listener):
        try:
            self.event_listeners_count -= 1
            self.event_listeners.remove(listener)
            self.logger.debug('django-sockjs-server(PikaClient): listener %s removed' % repr(listener))
        except KeyError:
            pass

    def add_subscriber_channel(self, conn_id, room, client):
        self.subscrib_channel[conn_id] = {'room': room, 'conn': client}
        self.logger.debug('django-sockjs-server(PikaClient): listener %s add to room %s' % (repr(client), room))

    def remove_subscriber_channel(self, conn_id, client):
        try:
            room = self.subscrib_channel[conn_id].get('room')
            del self.subscrib_channel[conn_id]
            self.logger.debug('django-sockjs-server(PikaClient): listener %s del connection %s from room %s' % (repr(client),
                              conn_id, room))
        except KeyError:
            pass

    def get_event_listeners_count(self):
        return self.event_listeners_count

    def get_subscribe_channel_count(self):
        return len(self.subscrib_channel.keys())

    def get_subscribe_channels(self):
        return self.subscrib_channel.keys()

    def get_last_reconnect(self):
        return self.last_reconnect

    def get_uptime(self):
        return (now() - self.uptime_start).seconds
