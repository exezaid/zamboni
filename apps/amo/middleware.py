"""
Borrowed from: http://code.google.com/p/django-localeurl

Note: didn't make sense to use localeurl since we need to capture app as well
"""
import contextlib
import re
import time
import urllib

from django.conf import settings
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponsePermanentRedirect
from django.middleware import common
from django.utils.cache import patch_vary_headers
from django.utils.encoding import iri_to_uri, smart_str

import commonware.log
import MySQLdb as mysql
import tower
import jingo

import amo
from . import urlresolvers
from .helpers import urlparams


class LocaleAndAppURLMiddleware(object):
    """
    1. search for locale first
    2. see if there are acceptable apps
    3. save those matched parameters in the request
    4. strip them from the URL so we can do stuff
    """

    def process_request(self, request):
        # Find locale, app
        prefixer = urlresolvers.Prefixer(request)
        urlresolvers.set_url_prefix(prefixer)
        full_path = prefixer.fix(prefixer.shortened_path)

        if 'lang' in request.GET:
            # Blank out the locale so that we can set a new one.  Remove lang
            # from query params so we don't have an infinite loop.
            prefixer.locale = ''
            new_path = prefixer.fix(prefixer.shortened_path)
            query = dict((smart_str(k), request.GET[k]) for k in request.GET)
            query.pop('lang')
            return HttpResponsePermanentRedirect(urlparams(new_path, **query))

        if full_path != request.path:
            query_string = request.META.get('QUERY_STRING', '')
            full_path = urllib.quote(full_path.encode('utf-8'))

            if query_string:
                full_path = "%s?%s" % (full_path, query_string)

            response = HttpResponsePermanentRedirect(full_path)

            # Vary on Accept-Language or User-Agent if we changed the locale or
            # app.
            old_app = prefixer.app
            old_locale = prefixer.locale
            new_locale, new_app, _ = prefixer.split_path(full_path)
            if old_locale != new_locale:
                patch_vary_headers(response, ['Accept-Language'])
            if old_app != new_app:
                patch_vary_headers(response, ['User-Agent'])
            return response

        request.path_info = '/' + prefixer.shortened_path
        tower.activate(prefixer.locale)
        request.APP = amo.APPS.get(prefixer.app)
        request.LANG = prefixer.locale


class NoVarySessionMiddleware(SessionMiddleware):
    """
    SessionMiddleware sets Vary: Cookie anytime request.session is accessed.
    request.session is accessed indirectly anytime request.user is touched.
    We always touch request.user to see if the user is authenticated, so every
    request would be sending vary, so we'd get no caching.

    We skip the cache in Zeus if someone has an AMOv3 cookie, so varying on
    Cookie at this level only hurts us.
    """

    def process_response(self, request, response):
        if settings.READ_ONLY:
            return response
        # Let SessionMiddleware do its processing but prevent it from changing
        # the Vary header.
        vary = response.get('Vary', None)
        new_response = (super(NoVarySessionMiddleware, self)
                        .process_response(request, response))
        if vary:
            new_response['Vary'] = vary
        else:
            del new_response['Vary']
        return new_response


class AMOAuthenticationMiddleware(AuthenticationMiddleware):
    def process_request(self, request):
        prefixes = request.path.split('/')
        if (len(prefixes) > 1 and prefixes[1]
            in settings.NO_AUTHENTICATION_PREFIX):
            request.user = AnonymousUser()
            return

        return (super(AMOAuthenticationMiddleware, self)
                .process_request(request))


class RemoveSlashMiddleware(object):
    """
    Middleware that tries to remove a trailing slash if there was a 404.

    If the response is a 404 because url resolution failed, we'll look for a
    better url without a trailing slash.
    """

    def process_response(self, request, response):
        if (response.status_code == 404
            and request.path_info.endswith('/')
            and not common._is_valid_path(request.path_info)
            and common._is_valid_path(request.path_info[:-1])):
            # Use request.path because we munged app/locale in path_info.
            newurl = request.path[:-1]
            if request.GET:
                with safe_query_string(request):
                    newurl += '?' + request.META['QUERY_STRING']
            return HttpResponsePermanentRedirect(newurl)
        else:
            return response


@contextlib.contextmanager
def safe_query_string(request):
    """
    Turn the QUERY_STRING into a unicode- and ascii-safe string.

    We need unicode so it can be combined with a reversed URL, but it has to be
    ascii to go in a Location header.  iri_to_uri seems like a good compromise.
    """
    qs = request.META['QUERY_STRING']
    try:
        request.META['QUERY_STRING'] = iri_to_uri(qs)
        yield
    finally:
        request.META['QUERY_STRING'] = qs


class CommonMiddleware(common.CommonMiddleware):

    def process_request(self, request):
        with safe_query_string(request):
            return super(CommonMiddleware, self).process_request(request)


class ReadOnlyMiddleware(object):

    def process_request(self, request):
        if request.method == 'POST':
            return jingo.render(request, 'amo/read-only.html', status=503)

    def process_exception(self, request, exception):
        if isinstance(exception, mysql.OperationalError):
            return jingo.render(request, 'amo/read-only.html', status=503)


timing_log = commonware.log.getLogger('z.timer')


class TimingMiddleware(object):

    def process_request(self, request):
        request._start = time.time()

    def process_response(self, request, response):
        auth = 'ANON'
        if hasattr(request, 'user') and request.user.is_authenticated():
            auth = 'AUTH'
        d = {'method': request.method, 'time': time.time() - request._start,
             'code': response.status_code, 'auth': auth,
             'url': smart_str(request.path_info)}
        msg = '{method} "{url}" ({code}) {time:.2f} [{auth}]'.format(**d)
        timing_log.info(msg)
        return response


# We do this in zeus for performance, so this exists as a POC and to work out
# the logic.
class DetectMobileMiddleware(object):
    # Mobile user agents.
    UA = re.compile('android|fennec|iemobile|iphone|opera (?:mini|mobi)')
    # We set a cookie if you explicitly select mobile/no mobile.
    MC = 'mamo'

    def process_request(self, request):
        ua = request.META.get('HTTP_USER_AGENT', '').lower()
        mc = request.COOKIES.get(self.MC)
        if (self.UA.search(ua) and mc != 'off') or mc == 'on':
            request.META['HTTP_X_MOBILE'] = '1'


class XMobileMiddleware(object):

    def redirect(self, request, base):
        path = base.rstrip('/') + request.path
        if request.GET:
            path += '?' + request.GET.urlencode()
        response = HttpResponsePermanentRedirect(path)
        response['Vary'] = 'X-Mobile'
        return response

    def process_request(self, request):
        try:
            want_mobile = int(request.META.get('HTTP_X_MOBILE', 0))
        except Exception:
            want_mobile = False
        # SERVER_NAME doesn't work on devserver, HOST cannot be trusted in
        # production.
        header = 'HTTP_HOST' if settings.DEBUG else 'SERVER_NAME'
        on_mobile = request.META[header] == settings.MOBILE_DOMAIN
        # TODO: check that we have a mobile page available.
        if want_mobile and not on_mobile:
            return self.redirect(request, settings.MOBILE_SITE_URL)
        if not want_mobile and on_mobile:
            return self.redirect(request, settings.SITE_URL)
        request.MOBILE = want_mobile

    def process_response(self, request, response):
        patch_vary_headers(response, ['X-Mobile'])
        return response
