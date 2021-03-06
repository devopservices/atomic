# Copyright (C) 2015 Red Hat, All rights reserved.
# AUTHORS: William Temple <wtemple@redhat.com>
#          Brent Baude    <bbaude@redhat.com>
#
# This library is a component of Project Atomic.
#
#    Project Atomic is free software; you can redistribute it and/or
#    modify it under the terms of the GNU General Public License as
#    published by the Free Software Foundation; either version 2 of
#    the License, or (at your option) any later version.
#
#    Project Atomic is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Project Atomic; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
#    02110-1301 USA.
#

import os

import docker
import json

import util

from fnmatch import fnmatch as matches

""" Module for mounting and unmounting containerized applications. """


class MountError(Exception):
    """Generic error mounting a candidate container."""
    def __init__(self, val):
        self.val = val

    def __str__(self):
        return str(self.val)


class SelectionMatchError(MountError):
    """Input identifier matched multiple mount candidates."""
    def __init__(self, i, matches):
        self.val = ('"{0}" matched multiple items. Try one of the following:\n'
                    '{1}'.format(i, '\n'.join(['\t' + m for m in matches])))


class Mount:
    """
    A class which contains backend-independent methods useful for mounting and
    unmounting containers.
    """
    def __init__(self, mountpoint, live=False):
        """
        Constructs the Mount class with a mountpoint.
        Optional: mount a running container live (read/write)
        """
        self.mountpoint = mountpoint
        self.live = live

    def mount(self, identifier, options=[]):
        raise NotImplementedError('Mount subclass does not implement mount() '
                                  'method.')

    def unmount(self):
        raise NotImplementedError('Mount subclass does not implement unmount()'
                                  ' method.')

    # LVM DeviceMapper Utility Methods
    @staticmethod
    def _activate_thin_device(name, dm_id, size, pool):
        """
        Provisions an LVM device-mapper thin device reflecting,
        DM device id 'dm_id' in the docker pool.
        """
        table = '0 {0} thin /dev/mapper/{1} {2}'.format(int(size)/512,
                                                        pool, dm_id)
        cmd = ['dmsetup', 'create', name, '--table', table]
        r = util.subp(cmd)
        if r.return_code != 0:
            raise MountError('Failed to create thin device: ' + r.stderr)

    @staticmethod
    def _remove_thin_device(name):
        """
        Destroys a thin device via subprocess call.
        """
        r = util.subp(['dmsetup', 'remove', name])
        if r.return_code != 0:
            raise MountError('Could not remove thin device:\n' + r.stderr)

    @staticmethod
    def _is_device_active(device):
        """
        Checks dmsetup to see if a device is already active
        """
        cmd = ['dmsetup', 'info', device]
        dmsetup_info = util.subp(cmd)
        for dm_line in dmsetup_info.stdout.split("\n"):
            line = dm_line.split(':')
            if ('State' in line[0].strip()) and ('ACTIVE' in line[1].strip()):
                return True
        return False

    @staticmethod
    def _get_fs(thin_pathname):
        """
        Returns the file system type (xfs, ext4) of a given device
        """
        cmd = ['lsblk', '-o', 'FSTYPE', '-n', thin_pathname]
        fs_return = util.subp(cmd)
        return fs_return.stdout.strip()

    @staticmethod
    def mount_path(source, target, optstring='', bind=False):
        """
        Subprocess call to mount dev at path.
        """
        cmd = ['mount']
        if bind:
            cmd.append('--bind')
        if optstring:
            cmd.append('-o')
            cmd.append(optstring)
        cmd.append(source)
        cmd.append(target)
        r = util.subp(cmd)
        if r.return_code != 0:
            raise MountError('Could not mount docker container:\n' +
                             ' '.join(cmd) + '\n' + r.stderr)

    @staticmethod
    def get_dev_at_mountpoint(mntpoint):
        """
        Retrieves the device mounted at mntpoint, or raises
        MountError if none.
        """
        results = util.subp(['findmnt', '-o', 'SOURCE', mntpoint])
        if results.return_code != 0:
            raise MountError('No device mounted at ' + mntpoint)

        return results.stdout.replace('SOURCE\n', '').strip().split('\n')[-1]

    @staticmethod
    def unmount_path(path):
        """
        Unmounts the directory specified by path.
        """
        r = util.subp(['umount', path])
        if r.return_code != 0:
            raise ValueError(r.stderr)


class DockerMount(Mount):
    """
    A class which can be used to mount and unmount docker containers and
    images on a filesystem location.
    """
    def __init__(self, mountpoint, live=False):
        Mount.__init__(self, mountpoint, live)
        self.client = docker.Client()

    def _create_temp_container(self, iid):
        """
        Create a temporary container from a given iid.

        Temporary containers are marked with a sentinel environment
        variable so that they can be cleaned on unmount.
        """
        try:
            return self.client.create_container(
                                 image=iid, command='/bin/true',
                                 environment=['_ATOMIC_TEMP_CONTAINER'],
                                 detach=True, mem_limit='4m',
                                 network_disabled=True)['Id']
        except docker.errors.APIError as ex:
            raise MountError('Error creating temporary container:\n' + str(ex))

    def _clone(self, cid):
        """
        Create a temporary image snapshot from a given cid.

        Temporary image snapshots are marked with a sentinel label
        so that they can be cleaned on unmount.
        """
        try:
            iid = self.client.commit(
                    container=cid,
                    conf={
                            'Labels': {
                                'io.projectatomic.Temporary': 'true'
                            }
                        }
                    )['Id']
        except docker.errors.APIError as ex:
            raise MountError(str(ex))
        return self._create_temp_container(iid)

    def _identifier_as_cid(self, identifier):
        """
        Returns a container uuid for identifier.

        If identifier is an image UUID or image tag, create a temporary
        container and return its uuid.
        """
        def __cname_matches(container, identifier):
            return any([n for n in container['Names']
                        if matches(n, '/' + identifier)])

        # Determine if identifier is a container
        containers = [c['Id'] for c in self.client.containers(all=True)
                      if (__cname_matches(c, identifier) or
                          matches(c['Id'], identifier + '*'))]

        if len(containers) > 1:
            raise SelectionMatchError(identifier, containers)
        elif len(containers) == 1:
            c = containers[0]
            return c if self.live else self._clone(c)

        # Determine if identifier is an image UUID
        images = [i for i in set(self.client.images(all=True, quiet=True))
                  if i.startswith(identifier)]

        if len(images) > 1:
            raise SelectionMatchError(identifier, images)
        elif len(images) == 1:
            return self._create_temp_container(images[0])

        # Match image tag.
        images = util.image_by_name(identifier)
        if len(images) > 1:
            tags = [t for i in images for t in i['RepoTags']]
            raise SelectionMatchError(identifier, tags)
        elif len(images) == 1:
            return self._create_temp_container(images[0]['Id'])

        raise MountError('{} did not match any image or container.'
                         ''.format(identifier))

    @staticmethod
    def _no_gd_api(cid):
        # TODO: Deprecated
        desc_file = os.path.join('/var/lib/docker/devicemapper/metadata', cid)
        desc = json.loads(open(desc_file).read())
        return desc['device_id'], desc['size']

    def mount(self, identifier, options=[]):
        """
        Mounts a container or image referred to by identifier to
        the host filesystem.
        """
        driver = self.client.info()['Driver']
        driver_mount_fn = getattr(self, "_mount_" + driver,
                                  self._unsupported_backend)

        driver_mount_fn(identifier, options)

    def _unsupported_backend(self, identifier, options):
        raise MountError('Atomic mount is not supported on the {} docker '
                         'storage backend.'
                         ''.format(self.client.info()['Driver']))

    def _default_options(self, options, default_con=None, default_options=[]):
        """
        Merges user options with default options and determines security
        context.
        """
        if not options:
            options = default_options
        # Determines default context.
        if all([o.find('context=') == -1 for o in options]):
            options.append('context="' +
                           (default_con if default_con else
                            util.default_container_context()) + '"')
        return options

    def _mount_devicemapper(self, identifier, options):
        """
        Devicemapper mount backend.
        """
        if os.geteuid() != 0:
            raise MountError('Insufficient privileges to mount device.')

        if self.live and options:
            raise MountError('Cannot set mount options for live container '
                             'mount.')

        info = self.client.info()

        cid = self._identifier_as_cid(identifier)
        cinfo = self.client.inspect_container(cid)

        if self.live and not cinfo['State']['Running']:
                self._cleanup_container(cinfo)
                raise MountError('Cannot live mount non-running container.')

        options = self._default_options(
                options, default_con=cinfo['MountLabel'],
                default_options=[] if self.live else ['ro', 'nosuid', 'nodev'])

        dm_dev_name, dm_dev_id, dm_dev_size = '', '', ''
        dm_pool = info['DriverStatus'][0][1]

        if 'GraphDriver' in cinfo:
            dm_dev_name = cinfo['GraphDriver']['Data']['DeviceName']
            dm_dev_id = cinfo['GraphDriver']['Data']['DeviceId']
            dm_dev_size = cinfo['GraphDriver']['Data']['DeviceSize']
        else:
            # TODO: deprecated when GraphDriver patch makes it upstream
            dm_dev_id, dm_dev_size = DockerMount._no_gd_api(cid)
            dm_dev_name = dm_pool.replace('pool', cid)

        dm_dev_path = os.path.join('/dev/mapper', dm_dev_name)
        # If the device isn't already there, activate it.
        if not os.path.exists(dm_dev_path):
            if self.live:
                raise MountError('Error: Attempted to live-mount unactivated '
                                 'device.')
            Mount._activate_thin_device(dm_dev_name, dm_dev_id, dm_dev_size,
                                        dm_pool)

        # XFS should get nosuid
        fstype = Mount._get_fs(dm_dev_path)
        if fstype.upper() == 'XFS' and 'suid' not in options:
            if 'nosuid' not in options:
                options.append('nosuid')

        try:
            Mount.mount_path(dm_dev_path, self.mountpoint,
                             optstring=(','.join(options)))
        except MountError as de:
            if not self.live:
                Mount._remove_thin_device(dm_dev_name)
            self._cleanup_container(cinfo)
            raise de

    def _cleanup_container(self, cinfo):
        """
        Remove a container and clean up its image if necessary.
        """
        # I'm not a fan of doing this again here.
        env = cinfo['Config']['Env']
        if (env and '_ATOMIC_TEMP_CONTAINER' not in env) or not env:
            return

        iid = cinfo['Image']
        self.client.remove_container(cinfo['Id'])
        labels = self.client.inspect_image(iid)['Config']['Labels']
        # TODO: Config.Labels should be {} and not None if no labels.
        #       This prevents labels from being iterable if there are
        #       none.
        if labels and 'io.projectatomic.Temporary' in labels:
            if labels['io.projectatomic.Temporary'] == 'true':
                self.client.remove_image(iid)

    def unmount(self):
        """
        Unmounts and cleans-up after a previous mount().
        """
        driver = self.client.info()['Driver']
        driver_unmount_fn = getattr(self, "_unmount_" + driver,
                                    self._unsupported_backend)
        driver_unmount_fn()

    def _unmount_devicemapper(self):
        """
        Devicemapper unmount backend.
        """
        pool = self.client.info()['DriverStatus'][0][1]
        dev = Mount.get_dev_at_mountpoint(self.mountpoint)

        dev_name = dev.replace('/dev/mapper/', '')
        if not dev_name.startswith('docker-'):
            raise MountError('Device mounted at {} is not a docker container.'
                             ''.format(self.mountpoint))

        cid = dev_name.replace(pool.replace('pool', ''), '')
        try:
            self.client.inspect_container(cid)
        except docker.errors.APIError:
            raise MountError('Failed to associate device {0} mounted at {1} '
                             'with any container.'.format(dev_name,
                                                          self.mountpoint))

        Mount.unmount_path(self.mountpoint)
        cinfo = self.client.inspect_container(cid)

        # Was the container live mounted? If so, done.
        # TODO: Container.Config.Env should be {} (iterable) not None.
        #       Fix in docker-py.
        env = cinfo['Config']['Env']
        if (env and '_ATOMIC_TEMP_CONTAINER' not in env) or not env:
            return

        Mount._remove_thin_device(dev_name)
        self._cleanup_container(cinfo)
