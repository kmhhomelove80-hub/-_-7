import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from web_app import LocalServer
from web_runtime import Controller,MemoryVault


class LocalSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.controller=Controller(cls.temp.name,MemoryVault(),demo=True)
        cls.server=LocalServer(cls.controller)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.controller.close();cls.thread.join(2)
        cls.controller.telegram.thread.join(1);cls.controller.email.thread.join(1);cls.temp.cleanup()
    def request(self,path,data=None,token=True,origin=None,host=None):
        headers={'X-Local-Token':self.server.local_token} if token else {}
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        req=urllib.request.Request(self.server.origin+path,headers=headers,data=json.dumps(data).encode() if data is not None else None)
        return urllib.request.urlopen(req)
    def test_only_loopback_binding(self):self.assertEqual(self.server.server_address[0],'127.0.0.1')
    def test_unauthenticated_api_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:self.request('/api/state',token=False)
        self.assertEqual(caught.exception.code,403)
    def test_foreign_origin_mutation_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:self.request('/api/stop',{},origin='https://example.com')
        self.assertEqual(caught.exception.code,403)
    def test_rebound_hostname_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:self.request('/api/state',host='example.com')
        self.assertEqual(caught.exception.code,403)
    def test_demo_cannot_start_even_with_valid_local_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:self.request('/api/start',{},origin=self.server.origin)
        self.assertEqual(caught.exception.code,409)
    def test_static_security_headers_and_no_secret_in_html(self):
        response=self.request('/')
        self.assertEqual(response.headers['X-Frame-Options'],'DENY')
        self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
        self.assertNotIn(self.server.local_token,response.read().decode())
    def test_path_traversal_not_served(self):
        with self.assertRaises(urllib.error.HTTPError):self.request('/../core.py')
    def test_authorized_state_can_be_read_without_secrets(self):
        data=json.load(self.request('/api/state'))
        self.assertTrue(data['demo']);self.assertFalse(data['running'])
        self.assertNotIn('secret',data)
