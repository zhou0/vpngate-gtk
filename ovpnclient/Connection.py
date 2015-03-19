import asyncore
import os
import subprocess
import tempfile
import threading

from gi.repository import GObject

from .AsyncManagerHandler import AsyncManagerHandler


(
    STATE_DISCONNECTED,
    STATE_CONNECTING,
    STATE_AUTHENTICATING,
    STATE_GETTINGCONFIG,
    STATE_GOTIPADDR,
    STATE_CONNECTED,
    STATE_DISCONNECTING,
    STATE_FAILED,
) = range(8)

TXT_STATES = {
    STATE_DISCONNECTED      : 'Disconnected',
    STATE_CONNECTING        : 'Connecting',
    STATE_AUTHENTICATING    : 'Authenticating',
    STATE_GETTINGCONFIG     : 'Getting configuration',
    STATE_GOTIPADDR         : 'Got network parameters',
    STATE_CONNECTED         : 'Connected',
    STATE_DISCONNECTING     : 'Disconnecting',
    STATE_FAILED            : 'Error',
}


class Connection(object):
    def __init__(self, config=None, opencb=None, closecb=None, tries=6, interval=2):
        self.config = config
        self.state = STATE_DISCONNECTED
        self.handler = None
        self.port = 10598
        self.logbuf = []
        self.tmpfile = None
        self.openCallback = opencb
        self.closeCallback = closecb
        self._tries = tries
        self.interval = interval
        self.running = False
        self.proces = None
        self.vpnipaddr = None
        self.vpnconnectstatus = None


    def get_state(self):
        return self.state


    def set_state(self, state):
        prev = self.state
        self.state = state

        if self.state != prev:
            self.on_state_change()


    def get_state_str(self):
        return TXT_STATES[self.state]


    def set_open_callback(self, callback):
        self.openCallback = callback


    def set_close_callback(self, callback):
        self.closeCallback = callback


    def set_config(self, config):
        self.config = config
        return self


    def open(self):
        tmpfile = tempfile.NamedTemporaryFile(delete=False)
        tmpfile.write(self.config)

        # weak attempt at cross-platform
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        self.proces = subprocess.Popen(
            [
                '/usr/bin/pkexec',
                'openvpn',
                '--config', tmpfile.name,
                '--management', '127.0.0.1', str(self.port),
                '--management-query-passwords',
                '--management-log-cache', '200',
                '--management-hold'
            ],
            startupinfo=startupinfo,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE
        )

        self.running = False
        self.tries = self._tries
        #GObject.timeout_add(self.interval*1000, self.check_openvpn_running)
        GObject.idle_add(self.check_openvpn_running)


    def check_openvpn_running(self):
        print('Waiting for OpenVPN to start running... ', end='')
        line = self.proces.stdout.readline()
        if line:
            if 'Error executing command' in str(line):
                print('Error, couldn\'t get admin access')
            else:
                print('OK!')
                self.running = True
                self.tries = self._tries*10
                GObject.timeout_add(100, self.check_openvpn_listening)
            return False
        else:
            self.tries -= 1
            return self.process is not None and self.process.poll() is None and self.tries > 0


    def check_openvpn_listening(self):
        print('Checking if OpenVPN is listening... ', end='')
        line = self.proces.stdout.readline()
        if line:
            text = str(line)
            if 'MANAGEMENT: TCP Socket listening' in text:
                print('OK!')
                self.handler = AsyncManagerHandler(
                    self,
                    '127.0.0.1',
                    self.port,
                    closecb=self.closeCallback,
                    state_parser=self.parse_state
                )
                self.thread = threading.Thread(target=asyncore.loop, daemon=True, kwargs={'timeout':1})
                self.thread.start()
                return False
            elif 'MANAGEMENT: Socket bind failed on local address' in text:
                print('Port already in use, trying next one...')
                self.state = STATE_DISCONNECTED
                self.handler = None
                self.running = False
                self.proces = None
                self.port += 1
                self.open()
            else:
                return True
        else:
            self.tries -= 1
            return self.proces is not None and self.tries > 0


    def close(self):
        if self.handler is not None:
            self.handler.send(b'signal SIGTERM\n')
        self.thread.join()


    def on_open(self):
        self.state = STATE_DISCONNECTED
        print('OpenVPN:',TXT_STATES[self.state])

        if hasattr(self.openCallback, "__call__"):
            self.openCallback()


    def parse_state(self, line):
        # 1426785744,TCP_CONNECT,,,
        # 1426785745,WAIT,,,
        # 1426785745,AUTH,,,
        # 1426785748,GET_CONFIG,,,
        # 1426785749,ASSIGN_IP,,10.211.1.13,
        # 1426785750,CONNECTED,SUCCESS,10.211.1.13,1.249.243.61
        # 1426785794,EXITING,SIGTERM,,
        state_info = line.split(',')
        state = state_info[1]

        if state == 'TCP_CONNECT':
            self.set_state(STATE_CONNECTING)
        elif state == 'WAIT':
            self.set_state(STATE_CONNECTING)
        elif state == 'AUTH':
            self.set_state(STATE_AUTHENTICATING)
        elif state == 'GET_CONFIG':
            self.set_state(STATE_GETTINGCONFIG)
        elif state == 'ASSIGN_IP':
            self.vpnipaddr = state_info[3]
            self.set_state(STATE_GOTIPADDR)
        elif state == 'CONNECTED':
            self.vpnconnectstatus = state_info[2]
            self.set_state(STATE_CONNECTED)
        elif state == 'EXITING':
            self.set_state(STATE_DISCONNECTING)
        else:
            print('Unknown state:',line)
            self.set_state(STATE_FAILED)


    def on_state_change(self):
        print('OpenVPN:',TXT_STATES[self.state])


    def __exit__(self, *err):
        del self.handler
