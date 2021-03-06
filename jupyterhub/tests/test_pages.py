"""Tests for HTML pages"""

import asyncio
import sys
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
from tornado import gen
from tornado.httputil import url_concat

from ..handlers import BaseHandler
from ..utils import url_path_join as ujoin
from .. import orm
from ..auth import Authenticator

import mock
import pytest

from .mocking import FormSpawner, public_url, public_host
from .test_api import api_request, add_user
from .utils import async_requests


def get_page(path, app, hub=True, **kw):
    if hub:
        prefix = app.hub.base_url
    else:
        prefix = app.base_url
    base_url = ujoin(public_host(app), prefix)
    return async_requests.get(ujoin(base_url, path), **kw)


async def test_root_no_auth(app):
    url = ujoin(public_host(app), app.hub.base_url)
    r = await async_requests.get(url)
    r.raise_for_status()
    assert r.url == ujoin(url, 'login')


async def test_root_auth(app):
    cookies = await app.login_user('river')
    r = await async_requests.get(public_url(app), cookies=cookies)
    r.raise_for_status()
    assert r.url.startswith(public_url(app, app.users['river']))


async def test_root_redirect(app):
    name = 'wash'
    cookies = await app.login_user(name)
    next_url = ujoin(app.base_url, 'user/other/test.ipynb')
    url = '/?' + urlencode({'next': next_url})
    r = await get_page(url, app, cookies=cookies)
    r.raise_for_status()
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, 'user/%s/test.ipynb' % name)


async def test_root_default_url_noauth(app):
    with mock.patch.dict(app.tornado_settings,
                         {'default_url': '/foo/bar'}):
        r = await get_page('/', app, allow_redirects=False)
    r.raise_for_status()
    url = r.headers.get('Location', '')
    path = urlparse(url).path
    assert path == '/foo/bar'


async def test_root_default_url_auth(app):
    name = 'wash'
    cookies = await app.login_user(name)
    with mock.patch.dict(app.tornado_settings,
                         {'default_url': '/foo/bar'}):
        r = await get_page('/', app, cookies=cookies, allow_redirects=False)
    r.raise_for_status()
    url = r.headers.get('Location', '')
    path = urlparse(url).path
    assert path == '/foo/bar'


async def test_home_no_auth(app):
    r = await get_page('home', app, allow_redirects=False)
    r.raise_for_status()
    assert r.status_code == 302
    assert '/hub/login' in r.headers['Location']


async def test_home_auth(app):
    cookies = await app.login_user('river')
    r = await get_page('home', app, cookies=cookies)
    r.raise_for_status()
    assert r.url.endswith('home')


async def test_admin_no_auth(app):
    r = await get_page('admin', app)
    assert r.status_code == 403


async def test_admin_not_admin(app):
    cookies = await app.login_user('wash')
    r = await get_page('admin', app, cookies=cookies)
    assert r.status_code == 403


async def test_admin(app):
    cookies = await app.login_user('admin')
    r = await get_page('admin', app, cookies=cookies, allow_redirects=False)
    r.raise_for_status()
    assert r.url.endswith('/admin')


@pytest.mark.parametrize('sort', [
    'running',
    'last_activity',
    'admin',
    'name',
])
async def test_admin_sort(app, sort):
    cookies = await app.login_user('admin')
    r = await get_page('admin?sort=%s' % sort, app, cookies=cookies)
    r.raise_for_status()
    assert r.status_code == 200


async def test_spawn_redirect(app):
    name = 'wash'
    cookies = await app.login_user(name)
    u = app.users[orm.User.find(app.db, name)]

    status = await u.spawner.poll()
    assert status is not None

    # test spawn page when no server is running
    r = await get_page('spawn', app, cookies=cookies)
    r.raise_for_status()
    print(urlparse(r.url))
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, 'user/%s/' % name)

    # should have started server
    status = await u.spawner.poll()
    assert status is None

    # test spawn page when server is already running (just redirect)
    r = await get_page('spawn', app, cookies=cookies)
    r.raise_for_status()
    print(urlparse(r.url))
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, '/user/%s/' % name)

    # stop server to ensure /user/name is handled by the Hub
    r = await api_request(app, 'users', name, 'server', method='delete', cookies=cookies)
    r.raise_for_status()

    # test handing of trailing slash on `/user/name`
    r = await get_page('user/' + name, app, hub=False, cookies=cookies)
    r.raise_for_status()
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, '/user/%s/' % name)


async def test_spawn_handler_access(app):
    name = 'winston'
    cookies = await app.login_user(name)
    u = app.users[orm.User.find(app.db, name)]

    status = await u.spawner.poll()
    assert status is not None

    # spawn server via browser link with ?arg=value
    r = await get_page('spawn', app, cookies=cookies, params={'arg': 'value'})
    r.raise_for_status()

    # verify that request params got passed down
    # implemented in MockSpawner
    r = await async_requests.get(ujoin(public_url(app, u), 'env'))
    env = r.json()
    assert 'HANDLER_ARGS' in env
    assert env['HANDLER_ARGS'] == 'arg=value'

    # stop server
    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()


async def test_spawn_admin_access(app, admin_access):
    """GET /user/:name as admin with admin-access spawns user's server"""
    cookies = await app.login_user('admin')
    name = 'mariel'
    user = add_user(app.db, app=app, name=name)
    app.db.commit()
    r = await get_page('user/' + name, app, cookies=cookies)
    r.raise_for_status()
    assert (r.url.split('?')[0] + '/').startswith(public_url(app, user))
    r = await get_page('user/{}/env'.format(name), app, hub=False, cookies=cookies)
    r.raise_for_status()
    env = r.json()
    assert env['JUPYTERHUB_USER'] == name


async def test_spawn_page(app):
    with mock.patch.dict(app.users.settings, {'spawner_class': FormSpawner}):
        cookies = await app.login_user('jones')
        r = await get_page('spawn', app, cookies=cookies)
        assert r.url.endswith('/spawn')
        assert FormSpawner.options_form in r.text

        r = await get_page('spawn?next=foo', app, cookies=cookies)
        assert r.url.endswith('/spawn?next=foo')
        assert FormSpawner.options_form in r.text


async def test_spawn_page_admin(app, admin_access):
    with mock.patch.dict(app.users.settings, {'spawner_class': FormSpawner}):
        cookies = await app.login_user('admin')
        u = add_user(app.db, app=app, name='melanie')
        r = await get_page('spawn/' + u.name, app, cookies=cookies)
        assert r.url.endswith('/spawn/' + u.name)
        assert FormSpawner.options_form in r.text
        assert "Spawning server for {}".format(u.name) in r.text


async def test_spawn_form(app):
    with mock.patch.dict(app.users.settings, {'spawner_class': FormSpawner}):
        base_url = ujoin(public_host(app), app.hub.base_url)
        cookies = await app.login_user('jones')
        orm_u = orm.User.find(app.db, 'jones')
        u = app.users[orm_u]
        await u.stop()
        next_url = ujoin(app.base_url, 'user/jones/tree')
        r = await async_requests.post(
            url_concat(ujoin(base_url, 'spawn'), {'next': next_url}),
            cookies=cookies,
            data={'bounds': ['-1', '1'], 'energy': '511keV'},
        )
        r.raise_for_status()
        assert r.history
        assert u.spawner.user_options == {
            'energy': '511keV',
            'bounds': [-1, 1],
            'notspecified': 5,
        }


async def test_spawn_form_admin_access(app, admin_access):
    with mock.patch.dict(app.tornado_settings, {'spawner_class': FormSpawner}):
        base_url = ujoin(public_host(app), app.hub.base_url)
        cookies = await app.login_user('admin')
        u = add_user(app.db, app=app, name='martha')
        next_url = ujoin(app.base_url, 'user', u.name, 'tree')

        r = await async_requests.post(
            url_concat(ujoin(base_url, 'spawn', u.name), {'next': next_url}),
            cookies=cookies,
            data={'bounds': ['-3', '3'], 'energy': '938MeV'},
        )
        r.raise_for_status()
        assert r.history
        assert r.url.startswith(public_url(app, u))
        assert u.spawner.user_options == {
            'energy': '938MeV',
            'bounds': [-3, 3],
            'notspecified': 5,
        }


async def test_spawn_form_with_file(app):
    with mock.patch.dict(app.tornado_settings, {'spawner_class': FormSpawner}):
        base_url = ujoin(public_host(app), app.hub.base_url)
        cookies = await app.login_user('jones')
        orm_u = orm.User.find(app.db, 'jones')
        u = app.users[orm_u]
        await u.stop()

        r = await async_requests.post(ujoin(base_url, 'spawn'),
                          cookies=cookies,
                          data={
                              'bounds': ['-1', '1'],
                              'energy': '511keV',
                          },
                          files={'hello': ('hello.txt', b'hello world\n')}
                      )
        r.raise_for_status()
        assert u.spawner.user_options == {
            'energy': '511keV',
            'bounds': [-1, 1],
            'notspecified': 5,
            'hello': {'filename': 'hello.txt',
                      'body': b'hello world\n',
                      'content_type': 'application/unknown'},
        }


async def test_user_redirect(app):
    name = 'wash'
    cookies = await app.login_user(name)

    r = await get_page('/user-redirect/tree/top/', app)
    r.raise_for_status()
    print(urlparse(r.url))
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, '/hub/login')
    query = urlparse(r.url).query
    assert query == urlencode({
        'next': ujoin(app.hub.base_url, '/user-redirect/tree/top/')
    })

    r = await get_page('/user-redirect/notebooks/test.ipynb', app, cookies=cookies)
    r.raise_for_status()
    print(urlparse(r.url))
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, '/user/%s/notebooks/test.ipynb' % name)


async def test_user_redirect_deprecated(app):
    """redirecting from /user/someonelse/ URLs (deprecated)"""
    name = 'wash'
    cookies = await app.login_user(name)

    r = await get_page('/user/baduser', app, cookies=cookies, hub=False)
    r.raise_for_status()
    print(urlparse(r.url))
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, '/user/%s/' % name)

    r = await get_page('/user/baduser/test.ipynb', app, cookies=cookies, hub=False)
    r.raise_for_status()
    print(urlparse(r.url))
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, '/user/%s/test.ipynb' % name)

    r = await get_page('/user/baduser/test.ipynb', app, hub=False)
    r.raise_for_status()
    print(urlparse(r.url))
    path = urlparse(r.url).path
    assert path == ujoin(app.base_url, '/hub/login')
    query = urlparse(r.url).query
    assert query == urlencode({
        'next': ujoin(app.base_url, '/hub/user/baduser/test.ipynb')
    })


async def test_login_fail(app):
    name = 'wash'
    base_url = public_url(app)
    r = await async_requests.post(base_url + 'hub/login',
        data={
            'username': name,
            'password': 'wrong',
        },
        allow_redirects=False,
    )
    assert not r.cookies


async def test_login_strip(app):
    """Test that login form doesn't strip whitespace from passwords"""
    form_data = {
        'username': 'spiff',
        'password': ' space man ',
    }
    base_url = public_url(app)
    called_with = []
    @gen.coroutine
    def mock_authenticate(handler, data):
        called_with.append(data)

    with mock.patch.object(app.authenticator, 'authenticate', mock_authenticate):
        await async_requests.post(base_url + 'hub/login',
            data=form_data,
            allow_redirects=False,
        )

    assert called_with == [form_data]


@pytest.mark.parametrize(
    'running, next_url, location',
    [
        # default URL if next not specified, for both running and not
        (True, '', ''),
        (False, '', ''),
        # next_url is respected
        (False, '/hub/admin', '/hub/admin'),
        (False, '/user/other', '/hub/user/other'),
        (False, '/absolute', '/absolute'),
        (False, '/has?query#andhash', '/has?query#andhash'),

        # next_url outside is not allowed
        (False, 'https://other.domain', ''),
        (False, 'ftp://other.domain', ''),
        (False, '//other.domain', ''),
    ]
)
async def test_login_redirect(app, running, next_url, location):
    cookies = await app.login_user('river')
    user = app.users['river']
    if location:
        location = ujoin(app.base_url, location)
    else:
        # use default url
        location = user.url

    url = 'login'
    if next_url:
        if '//' not in next_url:
            next_url = ujoin(app.base_url, next_url, '')
        url = url_concat(url, dict(next=next_url))

    if running and not user.active:
        # ensure running
        await user.spawn()
    elif user.active and not running:
        # ensure not running
        await user.stop()
    r = await get_page(url, app, cookies=cookies, allow_redirects=False)
    r.raise_for_status()
    assert r.status_code == 302
    assert location == r.headers['Location']


async def test_auto_login(app, request):
    class DummyLoginHandler(BaseHandler):
        def get(self):
            self.write('ok!')
    base_url = public_url(app) + '/'
    app.tornado_application.add_handlers(".*$", [
        (ujoin(app.hub.base_url, 'dummy'), DummyLoginHandler),
    ])
    # no auto_login: end up at /hub/login
    r = await async_requests.get(base_url)
    assert r.url == public_url(app, path='hub/login')
    # enable auto_login: redirect from /hub/login to /hub/dummy
    authenticator = Authenticator(auto_login=True)
    authenticator.login_url = lambda base_url: ujoin(base_url, 'dummy')

    with mock.patch.dict(app.tornado_settings, {
        'authenticator': authenticator,
    }):
        r = await async_requests.get(base_url)
    assert r.url == public_url(app, path='hub/dummy')


async def test_auto_login_logout(app):
    name = 'burnham'
    cookies = await app.login_user(name)

    with mock.patch.dict(app.tornado_settings, {
        'authenticator': Authenticator(auto_login=True),
    }):
        r = await async_requests.get(public_host(app) + app.tornado_settings['logout_url'], cookies=cookies)
    r.raise_for_status()
    logout_url = public_host(app) + app.tornado_settings['logout_url']
    assert r.url == logout_url
    assert r.cookies == {}


async def test_logout(app):
    name = 'wash'
    cookies = await app.login_user(name)
    r = await async_requests.get(public_host(app) + app.tornado_settings['logout_url'], cookies=cookies)
    r.raise_for_status()
    login_url = public_host(app) + app.tornado_settings['login_url']
    assert r.url == login_url
    assert r.cookies == {}


@pytest.mark.parametrize('shutdown_on_logout', [True, False])
async def test_shutdown_on_logout(app, shutdown_on_logout):
    name = 'shutitdown'
    cookies = await app.login_user(name)
    user = app.users[name]

    # start the user's server
    await user.spawn()
    spawner = user.spawner

    # wait for any pending state to resolve
    for i in range(50):
        if not spawner.pending:
            break
        await asyncio.sleep(0.1)
    else:
        assert False, "Spawner still pending"
    assert spawner.active

    # logout
    with mock.patch.dict(app.tornado_settings, {
        'shutdown_on_logout': shutdown_on_logout,
    }):
        r = await async_requests.get(
            public_host(app) + app.tornado_settings['logout_url'],
            cookies=cookies,
        )
        r.raise_for_status()

    login_url = public_host(app) + app.tornado_settings['login_url']
    assert r.url == login_url
    assert r.cookies == {}

    # wait for any pending state to resolve
    for i in range(50):
        if not spawner.pending:
            break
        await asyncio.sleep(0.1)
    else:
        assert False, "Spawner still pending"

    assert spawner.ready == (not shutdown_on_logout)


async def test_login_no_whitelist_adds_user(app):
    auth = app.authenticator
    mock_add_user = mock.Mock()
    with mock.patch.object(auth, 'add_user', mock_add_user):
        cookies = await app.login_user('jubal')

    user = app.users['jubal']
    assert mock_add_user.mock_calls == [mock.call(user)]


async def test_static_files(app):
    base_url = ujoin(public_host(app), app.hub.base_url)
    r = await async_requests.get(ujoin(base_url, 'logo'))
    r.raise_for_status()
    assert r.headers['content-type'] == 'image/png'
    r = await async_requests.get(ujoin(base_url, 'static', 'images', 'datalayer-25.svg'))
    r.raise_for_status()
    assert r.headers['content-type'] == 'image/png'
    r = await async_requests.get(ujoin(base_url, 'static', 'css', 'style.min.css'))
    r.raise_for_status()
    assert r.headers['content-type'] == 'text/css'


async def test_token_auth(app):
    cookies = await app.login_user('token')
    r = await get_page('token', app, cookies=cookies)
    r.raise_for_status()
    assert r.status_code == 200


async def test_oauth_token_page(app):
    name = 'token'
    cookies = await app.login_user(name)
    user = app.users[orm.User.find(app.db, name)]
    client = orm.OAuthClient(identifier='token')
    app.db.add(client)
    oauth_token = orm.OAuthAccessToken(client=client, user=user, grant_type=orm.GrantType.authorization_code)
    app.db.add(oauth_token)
    app.db.commit()
    r = await get_page('token', app, cookies=cookies)
    r.raise_for_status()
    assert r.status_code == 200


@pytest.mark.parametrize("error_status", [
    503,
    404,
])
async def test_proxy_error(app, error_status):
    r = await get_page('/error/%i' % error_status, app)
    assert r.status_code == 200


@pytest.mark.parametrize(
    "announcements",
    [
        "",
        "spawn",
        "spawn,home,login",
        "login,logout",
    ]
)
async def test_announcements(app, announcements):
    """Test announcements on various pages"""
    # Default announcement - same on all pages
    ann01 = "ANNOUNCE01"
    template_vars = {"announcement": ann01}
    announcements = announcements.split(",")
    for name in announcements:
        template_vars["announcement_" + name] = "ANN_" + name

    def assert_announcement(name, text):
        if name in announcements:
            assert template_vars["announcement_" + name] in text
            assert ann01 not in text
        else:
            assert ann01 in text

    cookies = await app.login_user("jones")

    with mock.patch.dict(
        app.tornado_settings,
        {"template_vars": template_vars, "spawner_class": FormSpawner},
    ):
        r = await get_page("login", app)
        r.raise_for_status()
        assert_announcement("login", r.text)
        r = await get_page("spawn", app, cookies=cookies)
        r.raise_for_status()
        assert_announcement("spawn", r.text)
        r = await get_page("home", app, cookies=cookies)  # hub/home
        r.raise_for_status()
        assert_announcement("home", r.text)
        # need auto_login=True to get logout page
        auto_login = app.authenticator.auto_login
        app.authenticator.auto_login = True
        try:
            r = await get_page("logout", app, cookies=cookies)
        finally:
            app.authenticator.auto_login = auto_login
        r.raise_for_status()
        assert_announcement("logout", r.text)


@pytest.mark.parametrize(
    "params",
    [
        "",
        "redirect_uri=/noexist",
        "redirect_uri=ok&client_id=nosuchthing",
    ]
)
async def test_bad_oauth_get(app, params):
    cookies = await app.login_user("authorizer")
    r = await get_page("hub/api/oauth2/authorize?" + params, app, hub=False, cookies=cookies)
    assert r.status_code == 400


async def test_token_page(app):
    name = "cake"
    cookies = await app.login_user(name)
    r = await get_page("token", app, cookies=cookies)
    r.raise_for_status()
    assert urlparse(r.url).path.endswith('/hub/token')
    def extract_body(r):
        soup = BeautifulSoup(r.text, "html5lib")
        import re
        # trim empty lines
        return re.sub(r"(\n\s*)+", "\n", soup.body.find(class_="container").text)
    body = extract_body(r)
    assert "Request new API token" in body, body
    # no tokens yet, no lists
    assert "API Tokens" not in body, body
    assert "Authorized Applications" not in body, body

    # request an API token
    user = app.users[name]
    token = user.new_api_token(expires_in=60, note="my-test-token")
    app.db.commit()

    r = await get_page("token", app, cookies=cookies)
    r.raise_for_status()
    body = extract_body(r)
    assert "API Tokens" in body, body
    assert "my-test-token" in body, body
    # no oauth tokens yet, shouldn't have that section
    assert "Authorized Applications" not in body, body

    # spawn the user to trigger oauth, etc.
    # request an oauth token
    user.spawner.cmd = [sys.executable, '-m', 'jupyterhub.singleuser']
    r = await get_page("spawn", app, cookies=cookies)
    r.raise_for_status()

    r = await get_page("token", app, cookies=cookies)
    r.raise_for_status()
    body = extract_body(r)
    assert "API Tokens" in body, body
    assert "Server at %s" % user.base_url in body, body
    assert "Authorized Applications" in body, body


async def test_server_not_running_api_request(app):
    cookies = await app.login_user("bees")
    r = await get_page("user/bees/api/status", app, hub=False, cookies=cookies)
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/json"
    assert r.json() == {"message": "bees is not running"}


async def test_metrics_no_auth(app):
    r = await get_page("metrics", app)
    assert r.status_code == 403


async def test_metrics_auth(app):
    cookies = await app.login_user('river')
    metrics_url = ujoin(public_host(app), app.hub.base_url, 'metrics')
    r = await get_page("metrics", app, cookies=cookies)
    assert r.status_code == 200
    assert r.url == metrics_url


async def test_health_check_request(app):
    r = await get_page('health', app)
    assert r.status_code == 200
