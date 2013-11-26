import unittest

import mock

import uvtool.libvirt.simplestreams as simplestreams

FAKE_VOLUME_PRODUCT_NAME = 'com.ubuntu.cloud:server:12.04:amd64'
FAKE_VOLUME_VERSION_0 = '20131119'
FAKE_VOLUME_VERSION_1 = '20131120'
ENCODED_FAKE_VOLUME_PRODUCT_NAME_0 = simplestreams._encode_libvirt_pool_name(
    FAKE_VOLUME_PRODUCT_NAME, FAKE_VOLUME_VERSION_0)
ENCODED_FAKE_VOLUME_PRODUCT_NAME_1 = simplestreams._encode_libvirt_pool_name(
    FAKE_VOLUME_PRODUCT_NAME, FAKE_VOLUME_VERSION_1)

@mock.patch('uvtool.libvirt.simplestreams.uvtool.libvirt')
@mock.patch('uvtool.libvirt.simplestreams.pool_metadata', new={})
@mock.patch('uvtool.libvirt.simplestreams.libvirt')
class TestSimpleStreams(unittest.TestCase):
    def testSync(self, libvirt, uvtool_libvirt):
        uvtool_libvirt.have_volume_by_name.return_value = False
        uvtool_libvirt.get_all_domain_volume_names.return_value = []
        uvtool_libvirt.volume_names_in_pool.return_value = [
            ENCODED_FAKE_VOLUME_PRODUCT_NAME_0]
        simplestreams.main(
            'sync '
            '--no-authentication '
            '--source=uvtool/tests/streams/fake_stream_0 '
            '--path streams/v1/index.json '
            .split()
        )
        # Check that we have mocked libvirt correctly, which means that
        # this test is working. We expect libvirt.open to have been called at
        # least once by uvtool.libvirt.simplestreams directly. This is more of
        # an assertion about the test being correct than part of the test
        # itself.
        libvirt.assert_has_calls(mock.call.open(u'qemu:///system'))

        # create_volume_from_fobj should have been called exactly once to
        # create the volume with the name that we expect
        self.assertEqual(uvtool_libvirt.create_volume_from_fobj.call_count, 1)
        self.assertEqual(
            uvtool_libvirt.create_volume_from_fobj.call_args[0][0],
            ENCODED_FAKE_VOLUME_PRODUCT_NAME_0
        )
        # Make sure the only calls to uvtool.libvirt were ones that we have
        # either whitelisted to be query-only (no side effects), or that we
        # are checking already. This makes sure, for example, that we aren't
        # deleting any volumes that we aren't expecting to delete.
        for call in uvtool_libvirt.mock_calls:
            name = call[0]
            self.assertIn(name, [
                # side effects that we have checked already
                'get_all_domain_volume_names',

                # whitelist of query functions that produce no side effects
                'create_volume_from_fobj',
                'get_libvirt_pool_object',
                'have_volume_by_name',
                'volume_names_in_pool',
            ])

    def _testResync(self, libvirt, uvtool_libvirt, old_volume_delete_expected,
            volumes_in_use=None):
        uvtool_libvirt.have_volume_by_name.side_effect = (
            lambda name, **kwargs: name == ENCODED_FAKE_VOLUME_PRODUCT_NAME_0)
        if volumes_in_use:
            uvtool_libvirt.get_all_domain_volume_names.return_value = list(
                volumes_in_use)
        else:
            uvtool_libvirt.get_all_domain_volume_names.return_value = []
        uvtool_libvirt.volume_names_in_pool.return_value = [
            ENCODED_FAKE_VOLUME_PRODUCT_NAME_0]
        simplestreams.main(
            'sync '
            '--no-authentication '
            '--source=uvtool/tests/streams/fake_stream_0 '
            '--path streams/v1/index.json '
            .split()
        )
        uvtool_libvirt.reset_mock()
        uvtool_libvirt.volume_names_in_pool.return_value = [
            ENCODED_FAKE_VOLUME_PRODUCT_NAME_0]
        simplestreams.main(
            'sync '
            '--no-authentication '
            '--source=uvtool/tests/streams/fake_stream_1 '
            '--path streams/v1/index.json '
            .split()
        )
        # create_volume_from_fobj should have been called exactly once to
        # create the volume with the name that we expect
        self.assertEqual(uvtool_libvirt.create_volume_from_fobj.call_count, 1)
        self.assertEqual(
            uvtool_libvirt.create_volume_from_fobj.call_args[0][0],
            ENCODED_FAKE_VOLUME_PRODUCT_NAME_1
        )

        if old_volume_delete_expected:
            # delete_volume_by_name should have been called exactly once to
            # delete the old volume that now has a new version
            self.assertEqual(
                uvtool_libvirt.delete_volume_by_name.call_count, 1)
            self.assertEqual(
                uvtool_libvirt.delete_volume_by_name.call_args[0][0],
                ENCODED_FAKE_VOLUME_PRODUCT_NAME_0
            )
        else:
            # delete_volume_by_name should not have been called at all
            self.assertEqual(
                uvtool_libvirt.delete_volume_by_name.call_count, 0)

        # Make sure the only calls to uvtool.libvirt were ones that we have
        # either whitelisted to be query-only (no side effects), or that we
        # are checking already. This makes sure, for example, that we aren't
        # deleting any volumes that we aren't expecting to delete.
        for call in uvtool_libvirt.mock_calls:
            name = call[0]
            self.assertIn(name, [
                # delete volume checked already
                'delete_volume_by_name',
                #'get_libvirt_pool_object().storageVolLookupByName().delete',

                # side effects that we have checked already
                'get_all_domain_volume_names',

                # whitelist of query functions that produce no side effects
                'create_volume_from_fobj',
                'get_libvirt_pool_object',
                'have_volume_by_name',
                'volume_names_in_pool',
                #'get_libvirt_pool_object().storageVolLookupByName',
            ])

    def testResync(self, libvirt, uvtool_libvirt):
        self._testResync(libvirt, uvtool_libvirt, True)

    def testResyncWithDomainUsingOldVolume(self, libvirt, uvtool_libvirt):
        self._testResync(
            libvirt,
            uvtool_libvirt,
            False,
            ['foo.qcow', 'foo-ds.qcow', ENCODED_FAKE_VOLUME_PRODUCT_NAME_0]
        )

