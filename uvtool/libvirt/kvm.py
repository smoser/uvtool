#!/usr/bin/python

# Wrapper around cloud-localds and libvirt

# Copyright (C) 2012-3 Canonical Ltd.
# Author: Robie Basak <robie.basak@canonical.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import errno
import functools
import itertools
import os
import shutil
import signal
import StringIO
import subprocess
import sys
import tempfile
import uuid
import yaml

import libvirt
from lxml import etree
from lxml.builder import E, ElementMaker

import uvtool.libvirt
from uvtool.libvirt import LIBVIRT_METADATA_XMLNS
import uvtool.libvirt.simplestreams
import uvtool.ssh
import uvtool.wait

DEFAULT_TEMPLATE = '/usr/share/uvtool/libvirt/template.xml'
DEFAULT_REMOTE_WAIT_SCRIPT = '/usr/share/uvtool/libvirt/remote-wait.sh'
POOL_NAME = 'uvtool'


class CLIError(Exception):
    """An error that should be reflected back to the CLI user."""
    pass


class InsecureError(RuntimeError):
    """An insecure operation is required and the user did not permit it by
    using --insecure."""
    pass


# From: http://www.chiark.greenend.org.uk/ucgi/~cjwatson/blosxom/2009-07-02-python-sigpipe.html
def subprocess_setup():
    # Python installs a SIGPIPE handler by default. This is usually not what
    # non-Python subprocesses expect.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def run_script_once_arg_to_config(arg, unique_id):
    with open(arg, 'rb') as f:
        script = f.read()
    encoded_script = script.encode('base64')
    return [
        b'cloud-init-per',
        b'once',
        unique_id.encode('utf-8'),
        b'sh', b'-c',
        (
            b'f=$(mktemp --tmpdir %s-XXXXXXXXXX) && ' +
            b'echo "%s" | base64 -d > "$f" && ' +
            b'chmod 700 "$f" && ' +
            b'"$f" && ' +
            b'rm "$f"'
        ) % (unique_id.encode('utf-8'), encoded_script)
    ]


def run_script_once_args_to_config(args):
    return [
        run_script_once_arg_to_config(arg, 'uvt-kvm-%s' % i)
        for i, arg in enumerate(args)
    ]


def get_ssh_agent_public_keys():
    """Read public keys from an agent, if available, or return None."""
    try:
        with open("/dev/null") as fpnull:
            output = subprocess.check_output(
                ['ssh-add', '-L'],
                stderr=fpnull
            )
    except subprocess.CalledProcessError:
        return None

    output = output.strip()
    if output:
        return output.splitlines()
    else:
        return None


def read_ssh_public_key_file(filename):
    """Read public keys from a file, or return None."""

    if filename is None:
        filename = os.path.join(os.environ['HOME'], '.ssh', 'id_rsa.pub')

    try:
        f = open(filename, 'rb')
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise
        return None, filename
    else:
        with f:
            return f.read().strip().splitlines(), filename


def get_ssh_authorized_keys(filename):
    # If the user hasn't explicitly specified a file source, then try
    # the agent first.
    if filename is None:
        agent_keys = get_ssh_agent_public_keys()
        if agent_keys:
            return agent_keys
        # Fall back to reading the public key file.

    # Read the public key file if one is present.
    file_keys, filename_used = read_ssh_public_key_file(filename)
    if file_keys:
        return file_keys
    else:
        print(
            "Warning: %s not found; instance will be started "
            "with no ssh access by default." % repr(filename_used),
            file=sys.stderr,
        )
        return []


def get_userdata_fobj(args, ssh_host_keys=None):
    """create a file object that contains user-data based on input"""
    if args.user_data and not args.add_user_data:
        # user provided '--user-data', but no '--add-user-data'
        return args.user_data

    fobjs = args.add_user_data
    if args.user_data:
        fobjs.insert(0, args.user_data)
        archive = []
    else:
        archive = get_internal_user_data(args, ssh_host_keys)

    for fobj in fobjs:
        archive.append(fobj.read())

    return StringIO.StringIO(
        '#cloud-config-archive\n' + yaml.safe_dump(archive))


def get_internal_user_data(args, ssh_host_keys=None):
    "return a string with a default cloud-init user-data blob"
    ssh_authorized_keys = get_ssh_authorized_keys(args.ssh_public_key_file)

    data = {
        b'hostname': args.hostname.encode('ascii'),
        b'manage_etc_hosts': b'localhost',
        b'snappy': {b'ssh_enabled': True},
        b'ssh_keys': uvtool.ssh.generate_ssh_host_keys()[0],
    }

    if ssh_host_keys:
        data[b'ssh_keys'] = ssh_host_keys

    if ssh_authorized_keys:
        data[b'ssh_authorized_keys'] = ssh_authorized_keys

    if args.password:
        data[b'password'] = args.password.encode('utf-8')
        data[b'chpasswd'] = {b'expire': False}
        data[b'ssh_pwauth'] = True

    if args.run_script_once:
        data[b'runcmd'] = run_script_once_args_to_config(args.run_script_once)

    if args.packages:
        data[b'packages'] = [
            s.encode('ascii')  # Debian Policy dictates a-z,0-9,+,-,.
            for s in itertools.chain(*[p.split(',') for p in args.packages])
        ]

    return ["#cloud-config\n" + yaml.safe_dump(data)]


def create_default_meta_data(fobj, args):
    data = {
        b'instance-id': str(uuid.uuid1()).encode('ascii'),
    }
    fobj.write(yaml.dump(data))


def create_ds_image(temp_dir, hostname, user_data_fobj, meta_data_fobj):
    """Create a file called ds.img inside temp_dir that contains a useful
    cloud-init data source.

    Other temporary files created in temp_dir are currently metadata and
    userdata and can be safely deleted.

    """

    with open(os.path.join(temp_dir, 'userdata'), 'wb') as f:
        f.write(user_data_fobj.read())
    with open(os.path.join(temp_dir, 'metadata'), 'wb') as f:
        f.write(meta_data_fobj.read())

    subprocess.check_call(
        ['cloud-localds', 'ds.img', 'userdata', 'metadata'], cwd=temp_dir)


def create_ds_volume(new_volume_name, hostname, user_data_fobj, meta_data_fobj):
    """Create a new libvirt cloud-init datasource volume."""

    temp_dir = tempfile.mkdtemp(prefix='uvt-kvm-')
    try:
        create_ds_image(temp_dir, hostname, user_data_fobj, meta_data_fobj)
        with open(os.path.join(temp_dir, 'ds.img'), 'rb') as f:
            return uvtool.libvirt.create_volume_from_fobj(
                new_volume_name, f, pool_name=POOL_NAME)
    finally:
        shutil.rmtree(temp_dir)


def create_cow_volume(backing_volume_name, new_volume_name, new_volume_size,
        conn=None):

    if conn is None:
        conn = libvirt.open('qemu:///system')

    pool = conn.storagePoolLookupByName(POOL_NAME)
    try:
        backing_vol = pool.storageVolLookupByName(backing_volume_name)
    except libvirt.libvirtError:
        raise RuntimeError("Cannot find volume %s" % backing_volume_name)

    return create_cow_volume_by_path(
        backing_volume_path=backing_vol.path(),
        new_volume_name=new_volume_name,
        new_volume_size=new_volume_size,
        conn=conn
    )

def create_cow_volume_by_path(backing_volume_path, new_volume_name,
        new_volume_size, conn=None):
    """Create a new libvirt qcow2 volume backed by an existing volume path."""

    if conn is None:
        conn = libvirt.open('qemu:///system')

    pool = conn.storagePoolLookupByName(POOL_NAME)

    new_vol = E.volume(
        E.name(new_volume_name),
        E.allocation('0'),
        E.capacity(str(new_volume_size), unit='G'),
        E.target(E.format(type='qcow2')),
        E.backingStore(
            E.path(backing_volume_path),
            E.format(type='qcow2'),
            )
        )
    return pool.createXML(etree.tostring(new_vol), 0)


def compose_domain_xml(name, volumes, cpu=1, memory=512, unsafe_caching=False,
        template_path=DEFAULT_TEMPLATE, log_console_output=False, bridge=None,
        ssh_known_hosts=None):
    tree = etree.parse(template_path)
    domain = tree.getroot()
    assert domain.tag == 'domain'

    etree.strip_elements(domain, 'name')
    etree.SubElement(domain, 'name').text = name

    etree.strip_elements(domain, 'vcpu')
    etree.SubElement(domain, 'vcpu').text = str(cpu)

    etree.strip_elements(domain, 'currentMemory')
    etree.SubElement(domain, 'currentMemory').text = str(memory * 1024)

    etree.strip_elements(domain, 'memory')
    etree.SubElement(domain, 'memory').text = str(memory * 1024)

    devices = domain.find('devices')

    etree.strip_elements(devices, 'disk')
    for disk_device, vol in zip(['vda', 'vdb'], volumes):
        disk_format_type = (
            etree.fromstring(vol.XMLDesc(0)).
            find('target').
            find('format').
            get('type')
            )
        if disk_format_type == "iso":
            disk_format_type = "raw"
        if unsafe_caching:
            disk_driver = E.driver(
                name='qemu', type=disk_format_type, cache='unsafe')
        else:
            disk_driver = E.driver(name='qemu', type=disk_format_type)
        devices.append(
            E.disk(
                disk_driver,
                E.source(file=vol.path()),
                E.target(dev=disk_device),
                type='file',
                device='disk',
                )
            )

    if bridge:
        etree.strip_elements(devices, 'interface')
        devices.append(E.interface(
                         E.source(bridge=bridge),
                         E.model(type='virtio'),
                         type='bridge'),
                      )

    if log_console_output:
        print(
            "Warning: logging guest console output introduces a DoS " +
                "security problem on the host and should not be used in " +
                "production.",
            file=sys.stderr
        )
        etree.strip_elements(devices, 'serial')
        devices.append(E.serial(E.target(port='0'), type='stdio'))

    if ssh_known_hosts:
        metadata = domain.find('metadata')
        if metadata is None:
            metadata = E.metadata()
            domain.append(metadata)
        EX = ElementMaker(
            namespace=LIBVIRT_METADATA_XMLNS,
            nsmap={'uvt': LIBVIRT_METADATA_XMLNS}
        )
        metadata.append(EX.ssh_known_hosts(ssh_known_hosts))

    return etree.tostring(tree)


def get_base_image(filters):
    result = list(uvtool.libvirt.simplestreams.query(filters))
    if not result:
        raise CLIError(
            "no images found that match filters %s." % repr(filters))
    elif len(result) != 1:
        raise CLIError(
            "multiple images found that match filters %s." % repr(filters))
    return result[0]


def create(hostname, filters, user_data_fobj, meta_data_fobj, memory=512,
           cpu=1, disk=2, unsafe_caching=False, template_path=DEFAULT_TEMPLATE,
           log_console_output=False, bridge=None, backing_image_file=None,
           ssh_known_hosts=None):
    if backing_image_file is None:
        base_volume_name = get_base_image(filters)
    undo_volume_creation = []
    try:
        # cow image names must end in ".qcow" so that the current Apparmor
        # profile for /usr/lib/libvirt/virt-aa-helper is able to read them,
        # determine their backing volumes, and generate a dynamic libvirt
        # profile that permits reading the backing volume. Once our pool
        # directory is added to the virt-aa-helper profile, this requirement
        # can be dropped.

        if backing_image_file:
            main_vol = create_cow_volume_by_path(
                backing_image_file, "%s.qcow" % hostname, disk)
        else:
            main_vol = create_cow_volume(
                base_volume_name, "%s.qcow" % hostname, disk)
        undo_volume_creation.append(main_vol)

        ds_vol = create_ds_volume(
            "%s-ds.qcow" % hostname, hostname, user_data_fobj, meta_data_fobj)
        undo_volume_creation.append(ds_vol)

        xml = compose_domain_xml(
            hostname, [main_vol, ds_vol],
            bridge=bridge,
            cpu=cpu,
            log_console_output=log_console_output,
            memory=memory,
            template_path=template_path,
            unsafe_caching=unsafe_caching,
            ssh_known_hosts=ssh_known_hosts,
        )
        conn = libvirt.open('qemu:///system')
        domain = conn.defineXML(xml)
        try:
            domain.create()
        except:
            domain.undefine()
            raise
    except:
        for vol in undo_volume_creation:
            vol.delete(0)
        raise


def delete_domain_volumes(conn, domain):
    """Delete all volumes associated with a domain.

    :param conn: libvirt connection object
    :param domain: libvirt domain object

    """
    domain_xml = etree.fromstring(domain.XMLDesc(0))
    assert domain_xml.tag == 'domain'
    for disk in domain_xml.find('devices').iter('disk'):
        disk_file = disk.find('source').get('file')
        vol = conn.storageVolLookupByKey(disk_file)
        vol.delete(0)


def destroy(hostname):
    conn = libvirt.open('qemu:///system')
    try:
        domain = conn.lookupByName(hostname)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            raise CLIError("domain %s not found." % repr(hostname))
        else:
            raise
    state = domain.state(0)[0]
    if state != libvirt.VIR_DOMAIN_SHUTOFF:
        domain.destroy()

    delete_domain_volumes(conn, domain)

    domain.undefine()


def get_lts_series():
    output = subprocess.check_output(['distro-info', '--lts'], close_fds=True)
    return output.strip()


def apply_default_fobj(args, key, create_default_data_fn):
    """Return the fobj specified, creating it if required.

    If the "key" attribute inside args is not None, then just return it.
    Otherwise, construct a new temporary fobj, populate it with the result of
    create_default_data_fn, and then return that.

    This is useful to apply default file objects to argparse.FileType command
    line parameters.

    """
    specified_fobj = getattr(args, key)
    if specified_fobj:
        return specified_fobj
    else:
        default_fobj = StringIO.StringIO()
        create_default_data_fn(default_fobj, args)
        default_fobj.seek(0)
        return default_fobj


def check_kvm_ok():
    try:
        process = subprocess.Popen(
            ['kvm-ok'], shell=False, stdout=subprocess.PIPE, close_fds=True)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise
        # Ignore if we can't find kvm-ok executable
        return True, None
    stdout, stderr = process.communicate()
    return (False, stdout) if process.returncode else (True, None)


def name_to_ips(name):
    macs = uvtool.libvirt.get_domain_macs(name)
    return [
        ip for ip
        in (uvtool.libvirt.mac_to_ip(mac) for mac in macs)
        if ip
    ]


def ssh(name, login_name, arguments, stdin=None, checked=False, sysexit=True,
        private_key_file=None, insecure=False):
    ips = name_to_ips(name)
    if len(ips) > 1:
        raise CLIError(
            "multiple IPs detected for %s %s and are not supported." %
                (repr(name), repr(ips))
        )
    ip = ips[0]

    objects_to_close = []
    try:
        ssh_call = [
            'ssh',
        ]

        ssh_known_hosts = uvtool.libvirt.get_domain_ssh_known_hosts(
            name, prefix=('%s ' % ip)
        )
        if ssh_known_hosts:
            ssh_known_hosts_file = tempfile.NamedTemporaryFile(
                prefix='uvt-kvm.known_hoststmp')
            objects_to_close.append(ssh_known_hosts_file)
            ssh_known_hosts_file.write(ssh_known_hosts)
            ssh_known_hosts_file.flush()
            ssh_call.extend(
                ['-o', 'UserKnownHostsFile=%s' % ssh_known_hosts_file.name]
            )
        else:
            if not insecure:
                raise InsecureError()
            ssh_call.extend([
                '-o', 'UserKnownHostsFile=/dev/null',
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'CheckHostIP=no',
            ])

        if login_name:
            ssh_call.extend(['-l', login_name])
        if private_key_file:
            ssh_call.extend(['-i', private_key_file])
        ssh_call.append(ip)
        ssh_call.extend(arguments)

        call = subprocess.check_call if checked else subprocess.call

        result = call(
            ssh_call, preexec_fn=subprocess_setup, close_fds=True, stdin=stdin
        )

        if sysexit:
            sys.exit(result)

        return result
    finally:
        [x.close() for x in objects_to_close]


def main_create(parser, args):
    if args.user_data and args.password:
        parser.error("--password cannot be used with --user-data.")
    if args.password:
        print(
            "Warning: using --password from the command line is " +
                "not secure and should be used for debugging only.",
            file=sys.stderr
        )

    if args.user_data and args.add_user_data:
        parser.error("--user-data and --add-user-data conflict")

    kvm_ok, is_kvm_ok_output = check_kvm_ok()
    if not kvm_ok:
        print(
            "KVM not available. kvm-ok returned:", is_kvm_ok_output,
            sep="\n", end="", file=sys.stderr
        )
        return

    ssh_host_keys, ssh_known_hosts = uvtool.ssh.generate_ssh_host_keys()

    user_data_fobj = get_userdata_fobj(args, ssh_host_keys=ssh_host_keys)
    meta_data_fobj = apply_default_fobj(
        args, 'meta_data', create_default_meta_data
    )
    if args.backing_image_file:
        abs_image_backing_file = os.path.abspath(args.backing_image_file)
    else:
        abs_image_backing_file = None
    create(
        args.hostname, args.filters, user_data_fobj, meta_data_fobj,
        backing_image_file=abs_image_backing_file,
        bridge=args.bridge,
        cpu=args.cpu,
        disk=args.disk,
        log_console_output=args.log_console_output,
        memory=args.memory,
        template_path=args.template,
        unsafe_caching=args.unsafe_caching,
        ssh_known_hosts=ssh_known_hosts,
    )

    if args.wait:
        args.name = args.hostname
        main_wait(parser, args)


def main_destroy(parser, args):
    for h in args.hostname:
        destroy(h)


def main_list(parser, args):
    # Hack for now. In time this should properly use the API and list
    # only instances created with this tool.
    subprocess.check_call('virsh -q list --all|awk \'{print $2}\'', shell=True)


def main_ip(parser, args):
    ips = name_to_ips(args.name)
    count = len(ips)
    if not count:
        raise CLIError(
            "no IP address found for libvirt machine %s." % repr(args.name))
    elif count > 1:
        print(
            "Warning: multiple IP address found for libvirt machine %s; " +
                "listing the first one only",
            file=sys.stderr
        )
    print(ips[0])


def main_ssh(parser, args, default_login_name='ubuntu'):
    if args.login_name:
        login_name = args.login_name
        name = args.name
    elif '@' in args.name:
        login_name, name = args.name.split('@', 1)
    else:
        login_name = default_login_name
        name = args.name

    try:
        return ssh(
            name, login_name, args.ssh_arguments, insecure=args.insecure)
    except InsecureError:
        raise CLIError(
            "ssh public host key not found. " +
                "Use --insecure iff you trust your network path to the guest."
        )


def main_wait_remote(parser, args):
    with open(args.remote_wait_script, 'rb') as wait_script:
        try:
            ssh(
                args.name,
                args.remote_wait_user,
                [
                    'env',
                    'UVTOOL_WAIT_INTERVAL=%s' % args.interval,
                    'UVTOOL_WAIT_TIMEOUT=%s' % args.timeout,
                    'sh',
                    '-'
                ],
                checked=True,
                stdin=wait_script,
                private_key_file=args.ssh_private_key_file,
                insecure=args.insecure,
            )
        except InsecureError:
            raise CLIError(
                "ssh public host key not found. Use "
                    "--insecure iff you trust your network path to the guest."
            )


def main_wait(parser, args):
    conn = libvirt.open('qemu:///system')
    domain = conn.lookupByName(args.name)
    state = domain.state(0)[0]
    if state != libvirt.VIR_DOMAIN_RUNNING:
        raise CLIError(
            "libvirt domain %s is not running." % repr(args.name))

    macs = list(uvtool.libvirt.get_domain_macs(args.name))
    if not macs:
        raise CLIError(
            "libvirt domain %s has no NIC MACs available." % repr(args.name))
    if len(macs) > 1:
        raise CLIError(
            "libvirt domain %s has more than one NIC defined."
                % repr(args.name)
        )
    mac = macs[0]
    if not uvtool.wait.wait_for_libvirt_dnsmasq_lease(
            mac, args.timeout):
        raise CLIError(
            "timed out waiting for dnsmasq lease for %s." % mac)
    host_ip = uvtool.libvirt.mac_to_ip(mac)
    if not uvtool.wait.wait_for_open_ssh_port(
            host_ip, args.interval, args.timeout):
        raise CLIError(
            "timed out waiting for ssh to open on %s." % host_ip)
    if not args.without_ssh:
        main_wait_remote(parser, args)


class DeveloperOptionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        namespace.unsafe_caching = True
        namespace.log_console_output = True


def main(args):
    # Workaround for https://bugzilla.redhat.com/show_bug.cgi?id=1063766
    # (LP: #1228231)
    libvirt.registerErrorHandler(lambda _: None, None)

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    wait_args = [
        (('--timeout',), {'type': float, 'default': 120.0}),
        (('--interval',), {'type': float, 'default': 1.0}),
        (('--remote-wait-script',), {'default': DEFAULT_REMOTE_WAIT_SCRIPT}),
        (('--insecure',), {'action': 'store_true'}),
        (('--remote-wait-user',), {'default': 'ubuntu'}),
        (('--without-ssh',), {'action': 'store_true'}),
        (('--ssh-private-key-file',), {}),
    ]

    create_subparser = subparsers.add_parser('create')
    create_subparser.set_defaults(func=main_create)
    create_subparser.add_argument(
        '--developer', '-d', nargs=0, action=DeveloperOptionAction)
    create_subparser.add_argument('--template', default=DEFAULT_TEMPLATE)
    create_subparser.add_argument('--memory', default=512, type=int)
    create_subparser.add_argument('--cpu', default=1, type=int)
    # TODO: reset this to 8GB after snappy images are smaller also TODO, fail
    # sanely if you try to create smaller than the source volume.
    create_subparser.add_argument('--disk', default=8, type=int,
                                  help='size of disk in GB')
    create_subparser.add_argument('--bridge')
    create_subparser.add_argument('--unsafe-caching', action='store_true')
    create_subparser.add_argument(
        '--user-data', type=argparse.FileType('rb'))
    create_subparser.add_argument(
        '--add-user-data', type=argparse.FileType('rb'), action='append',
        default=[], help='add additional user-data part')
    create_subparser.add_argument(
        '--meta-data', type=argparse.FileType('rb'))
    for w_args, w_kwargs in wait_args:
        create_subparser.add_argument(*w_args, **w_kwargs)

    create_subparser.add_argument('--password')
    create_subparser.add_argument('--log-console-output', action='store_true')
    create_subparser.add_argument('--backing-image-file')
    create_subparser.add_argument('--run-script-once', action='append')
    create_subparser.add_argument('--ssh-public-key-file')
    create_subparser.add_argument('--packages', action='append')
    create_subparser.add_argument('--wait', default=False, action='store_true')
    create_subparser.add_argument('hostname')
    create_subparser.add_argument(
        'filters', nargs='*', metavar='filter',
        default=["release=%s" % get_lts_series()],
    )
    destroy_subparser = subparsers.add_parser('destroy')
    destroy_subparser.set_defaults(func=main_destroy)
    destroy_subparser.add_argument('hostname', nargs='+')
    list_subparser = subparsers.add_parser('list')
    list_subparser.set_defaults(func=main_list)
    ip_subparser = subparsers.add_parser('ip')
    ip_subparser.set_defaults(func=main_ip)
    ip_subparser.add_argument('name')
    ssh_subparser = subparsers.add_parser('ssh')
    ssh_subparser.set_defaults(func=main_ssh)
    ssh_subparser.add_argument('--insecure', action='store_true')
    ssh_subparser.add_argument('--login-name', '-l')
    ssh_subparser.add_argument('name')
    ssh_subparser.add_argument('ssh_arguments', nargs='*')

    wait_subparser = subparsers.add_parser('wait')
    wait_subparser.set_defaults(func=main_wait)
    for w_args, w_kwargs in wait_args:
        wait_subparser.add_argument(*w_args, **w_kwargs)
    wait_subparser.add_argument('name')

    args = parser.parse_args(args)
    args.func(parser, args)


def main_cli_wrapper(*args, **kwargs):
    try:
        main(*args, **kwargs)
    except CLIError as e:
        print(
            "%s: error: %s" % (os.path.basename(sys.argv[0]), e),
            file=sys.stderr
        )
        sys.exit(1)
    except libvirt.libvirtError as e:
        libvirt_message = e.get_error_message()
        print(
            "%s: error: libvirt: %s" % (
                os.path.basename(sys.argv[0]),
                libvirt_message
            ),
            file=sys.stderr
        )
        sys.exit(1)


if __name__ == '__main__':
    main_cli_wrapper(sys.argv[1:])
