from nose.tools import eq_
import test_utils

from redisutils import mock_redis, reset_redis

import amo
from addons import cron
from addons.models import Addon, AppSupport
from addons.utils import ReverseNameLookup
from files.models import File


class TestBuildReverseNameLookup(test_utils.TestCase):
    fixtures = ('base/addon_3615',)

    def setUp(self):
        self._redis = mock_redis()

    def tearDown(self):
        reset_redis(self._redis)

    def test_lookup(self):
        cron.build_reverse_name_lookup()
        eq_(ReverseNameLookup.get('Delicious Bookmarks'), 3615)


class CurrentVersionTestCase(test_utils.TestCase):
    fixtures = ['base/addon_3615']

    def test_addons(self):
        Addon.objects.filter(pk=3615).update(_current_version=None)
        eq_(Addon.objects.filter(_current_version=None, pk=3615).count(), 1)
        cron._update_addons_current_version(((3615,),))
        eq_(Addon.objects.filter(_current_version=None, pk=3615).count(), 0)

    def test_cron(self):
        Addon.objects.filter(pk=3615).update(_current_version=None)
        eq_(Addon.objects.filter(_current_version=None, pk=3615).count(), 1)
        cron.update_addons_current_version()
        eq_(Addon.objects.filter(_current_version=None, pk=3615).count(), 0)


class TestLastUpdated(test_utils.TestCase):
    fixtures = ['base/addon_3615', 'addons/listed']

    def test_personas(self):
        Addon.objects.update(type=amo.ADDON_PERSONA, status=amo.STATUS_PUBLIC)

        cron.addon_last_updated()
        for addon in Addon.objects.all():
            eq_(addon.last_updated, addon.created)

        # Make sure it's stable.
        cron.addon_last_updated()
        for addon in Addon.objects.all():
            eq_(addon.last_updated, addon.created)

    def test_catchall(self):
        """Make sure the catch-all last_updated is stable and accurate."""
        # Nullify all datestatuschanged so the public add-ons hit the
        # catch-all.
        (File.objects.filter(status=amo.STATUS_PUBLIC)
         .update(datestatuschanged=None))
        Addon.objects.update(last_updated=None)

        cron.addon_last_updated()
        for addon in Addon.objects.filter(status=amo.STATUS_PUBLIC,
                                          type=amo.ADDON_EXTENSION):
            eq_(addon.last_updated, addon.created)

        # Make sure it's stable.
        cron.addon_last_updated()
        for addon in Addon.objects.filter(status=amo.STATUS_PUBLIC):
            eq_(addon.last_updated, addon.created)

    def test_last_updated_lite(self):
        # Make sure lite addons' last_updated matches their file's
        # datestatuschanged.
        Addon.objects.update(status=amo.STATUS_LITE, last_updated=None)
        File.objects.update(status=amo.STATUS_LITE)
        cron.addon_last_updated()
        addon = Addon.objects.get(id=3615)
        files = File.objects.filter(version__addon=addon)
        eq_(len(files), 1)
        eq_(addon.last_updated, files[0].datestatuschanged)
        assert addon.last_updated

    def test_last_update_lite_no_files(self):
        Addon.objects.update(status=amo.STATUS_LITE, last_updated=None)
        File.objects.update(status=amo.STATUS_UNREVIEWED)
        cron.addon_last_updated()
        addon = Addon.objects.get(id=3615)
        eq_(addon.last_updated, addon.created)
        assert addon.last_updated

    def test_appsupport(self):
        ids = Addon.objects.values_list('id', flat=True)
        cron._update_appsupport(ids)

        eq_(AppSupport.objects.count(), 2)

        # Run it again to test deletes.
        cron._update_appsupport(ids)
        eq_(AppSupport.objects.count(), 2)

    def test_appsupport_listed(self):
        AppSupport.objects.all().delete()
        eq_(AppSupport.objects.filter(addon=3723).count(), 0)
        cron.update_addon_appsupport()
        eq_(AppSupport.objects.filter(addon=3723, app=1).count(), 1)
