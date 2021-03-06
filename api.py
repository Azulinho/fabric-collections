# vim: ai ts=4 sts=4 et sw=4 ft=python fdm=indent et foldlevel=0
import json
import os
from fabric.api import env, sudo, local, execute, settings
from fabric.context_managers import hide
from time import sleep
from fabric.colors import green, yellow, red

from boto.ec2.blockdevicemapping import BlockDeviceMapping
from boto.ec2.blockdevicemapping import EBSBlockDeviceType

import socket


def add_epel_yum_repository():
    """ Install a repository that provides epel packages/updates """
    yum_install(packages=["epel-release"])


def add_firewalld_service(service, permanent=True):
    """ adds a firewall rule """
    yum_install(packages=['firewalld'])
    from fabric.api import settings
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=True, capture=True):
        p = ''
        if permanent:
            p = '--permanent'
        sudo('firewall-cmd --add-service %s %s' % (service, p))


def add_firewalld_port(port, permanent=True):
    """ adds a firewall rule """
    yum_install(packages=['firewalld'])
    from fabric.api import settings
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=True, capture=True):
        p = ''
        if permanent:
            p = '--permanent'
        sudo('firewall-cmd --add-port %s %s' % (port, p))


def add_zfs_yum_repository():
    """ adds the yum repository for ZFSonLinux """

    ZFS_REPO_PKG = (
        "http://archive.zfsonlinux.org/epel/zfs-release.el7.noarch.rpm"

    )
    yum_install_from_url('zfs-release', ZFS_REPO_PKG)


def arch():
    """ returns the current cpu archictecture """
    from fabric.api import settings
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=True, capture=True):
        result = sudo('rpm -E %dist').strip()
    return result


def cache_docker_image_locally(docker_image):
    # download docker images to speed up provisioning
    sudo("docker -q pull %s" % docker_image)


def connect_to_ec2():
    """ returns a connection object to AWS EC2  """
    import boto.ec2
    conn = boto.ec2.connect_to_region(env.ec2_region,
                                      aws_access_key_id=env.ec2_key,
                                      aws_secret_access_key=env.ec2_secret)
    return conn


def connect_to_rackspace():
    """ returns a connection object to Rackspace  """
    import pyrax

    pyrax.set_setting('identity_type', env.os_auth_system)
    pyrax.set_default_region(env.os_region_name)
    pyrax.set_credentials(env.os_username, env.os_password)
    nova = pyrax.connect_to_cloudservers(region=env.os_region_name)
    return nova


def create_ami(instance_id, name, description, block_device_mapping=None):
    conn = connect_to_ec2()
    ami = conn.create_image(instance_id,
                            name,
                            description,
                            block_device_mapping)

    image_status = conn.get_image(ami)
    while (image_status.state != "available" and
           image_status.state != "failed"):
        log_yellow('creating ami...')
        sleep_for_one_minute()
        image_status = conn.get_image(ami)

    if image_status.state == "available":
        log_green("ami %s %s" % (ami, image_status))
        return(ami)
    else:
        log_red("ami %s %s" % (ami, image_status))
        return False


def create_image(instance_id, name, description, block_device_mapping=None):
    """ proxy call for ec2, rackspace create ami backend functions """
    if env.cloud == 'ec2':
        return(create_ami(instance_id,
                          name,
                          description,
                          block_device_mapping=None))

    if env.cloud == 'rackspace':
        return(create_rackspace_image(instance_id,
                                      name,
                                      description,
                                      block_device_mapping=None))


def create_rackspace_image(server_id,
                           name,
                           description,
                           block_device_mapping=None):
    import sys
    if not is_there_state():
        log_red("can't find a valid state file")
        sys.exit(1)

    nova = connect_to_rackspace()
    data = load_state_from_disk()

    image_id = nova.servers.create_image(data['id'], name)
    image = nova.images.get(image_id).status.lower()
    log_green('creating rackspace image...')
    while nova.images.get(image_id).status.lower() not in ['active', 'error']:
        log_green('building rackspace image...')
        sleep_for_one_minute()

    if image == 'error':
        log_red('error creating image')
        sys.exit(1)

    return image_id


def create_docker_group():
    """ creates the docker group """
    from fabric.contrib.files import contains

    if not contains('/etc/group', 'docker', use_sudo=True):
        sudo("groupadd docker")


def create_server_ec2():
    """
    Creates EC2 Instance and saves it state in a local json file
    """
    # looks for an existing 'data.json' file, so that we don't start
    # additional ec2 instances when we don't need them.
    #
    if is_there_state():
        return True
    else:
        conn = connect_to_ec2()

        log_green("Started...")
        log_yellow("...Creating EC2 instance...")

        # we need a larger boot device to store our cached images
        dev_sda1 = EBSBlockDeviceType()
        dev_sda1.size = 250
        bdm = BlockDeviceMapping()
        bdm['/dev/sda1'] = dev_sda1

        # get an ec2 ami image object with our choosen ami
        image = conn.get_all_images(env.ec2_ami)[0]
        # start a new instance
        reservation = image.run(1, 1,
                                key_name=env.ec2_key_pair,
                                security_groups=env.ec2_security,
                                block_device_map=bdm,
                                instance_type=env.ec2_instancetype)

        # and get our instance_id
        instance = reservation.instances[0]
        # add a tag to our instance
        conn.create_tags([instance.id], {"name": 'jenkins-slave-img'})
        #  and loop and wait until ssh is available
        while instance.state == u'pending':
            log_yellow("Instance state: %s" % instance.state)
            sleep(10)
            instance.update()
        wait_for_ssh(instance.public_dns_name)

        log_green("Instance state: %s" % instance.state)
        log_green("Public dns: %s" % instance.public_dns_name)
        # finally save the details or our new instance into the local state file
        save_state_locally(instance.id)


def create_server_rackspace():
    """
    Creates Rackspace Instance and saves it state in a local json file
    """
    # looks for an existing 'data.json' file, so that we don't start
    # additional ec2 instances when we don't need them.
    #
    from time import sleep
    from sys import exit

    if is_there_state():
        return True

    # looks like no state file is available, lets create a new server instance
    nova = connect_to_rackspace()
    log_yellow("Creating Rackspace instance...")

    flavor = nova.flavors.find(name=env.rackspace_flavor)
    image = nova.images.find(name=env.rackspace_image)

    # nova.keypairs.create(env.rackspace_key_pair, env.rackspace_public_key)

    server = nova.servers.create(name=env.rackspace_instance_name,
                                 flavor=flavor.id,
                                 image=image.id,
                                 region=env.os_region_name,
                                 availability_zone=env.os_region_name,
                                 key_name=env.rackspace_key_pair)

    while server.status == 'BUILD':
        log_yellow("Waiting for build to finish...")
        sleep(5)
        server = nova.servers.get(server.id)

    # check for errors
    if server.status != 'ACTIVE':
        log_red("Error creating rackspace instance")
        exit(1)

    # the server was assigned IPv4 and IPv6 addresses, locate the IPv4 address
    ip_address = server.accessIPv4

    if ip_address is None:
        log_red('No IP address assigned')
        exit(1)

    wait_for_ssh(ip_address)
    log_green('New server with IP address {0}.'.format(ip_address))
    # finally save the details or our new instance into the local state file
    save_state_locally(server.id)


def disable_selinux():
    """ disables selinux """
    from fabric.contrib.files import sed, contains

    if contains(filename='/etc/selinux/config',
                text='SELINUX=enforcing'):
        sed('/etc/selinux/config',
            'SELINUX=enforcing', 'SELINUX=disabled', use_sudo=True)

    if contains(filename='/etc/selinux/config',
                text='SELINUXTYPE=enforcing'):
        sed('/etc/selinux/config',
            'SELINUXTYPE=enforcing', 'SELINUX=targeted', use_sudo=True)

    if sudo('getenforce') != 'Disabled':
        ec2_host = "%s@%s" % (env.user, load_state_from_disk()['ip_address'])
        execute(down, hosts=ec2_host)
        execute(up, hosts=ec2_host)


def does_container_exist(container):
    from fabric.api import settings
    with settings(warn_only=True):
        result = sudo('docker inspect %s' % container)
        print('*********************************************')
        log_red(result.return_code)
    if result.return_code is 0:
        return True
    else:
        return False


def destroy():
    if is_there_state():
        if env.cloud == 'ec2':
            destroy_ec2()
        if env.cloud == 'rackspace':
            destroy_rackspace()


def destroy_ec2():
    """ terminates the instance """
    if is_there_state() is False:
        return True
    else:
        conn = connect_to_ec2()
        _state = load_state_from_disk()
        data = get_ec2_info(_state['id'])
        instance = conn.terminate_instances(instance_ids=[data['id']])[0]
        log_yellow('destroying instance ...')
        while instance.state != "terminated":
            log_yellow("Instance state: %s" % instance.state)
            sleep(10)
            instance.update()
        volume = data['volume']
        if volume:
            log_yellow('destroying EBS volume ...')
            conn.delete_volume(volume)
        os.unlink('data.json')


def destroy_rackspace():
    """ terminates the instance """
    from time import sleep
    if is_there_state() is False:
        return True

    nova = connect_to_rackspace()
    _state = load_state_from_disk()
    server = nova.servers.get(_state['id'])
    log_yellow('deleting rackspace instance ...')
    server.delete()

    # wait for server to be deleted
    try:
        while True:
            server = nova.servers.get(server.id)
            log_yellow('waiting for deletion ...')
            sleep(5)
    except:
        pass
    log_green('The server has been deleted')


def does_image_exist(image):
    from fabric.api import settings
    with settings(warn_only=True):
        result = sudo('docker images')
        if image in result:
            return True
        else:
            return False


def down():
    halt()


def down_ec2():
    """ shutdown of an existing EC2 instance """
    conn = connect_to_ec2()
    # checks for a valid state file, containing the details our ec2 instance
    if is_there_state() is False:
        # we can't shutdown the instance, if we don't know which one it is
        return False
    else:
        # get the instance_id from the state file, and stop the instance
        data = load_state_from_disk()
        instance = conn.stop_instances(instance_ids=[data['id']])[0]
        while instance.state != "stopped":
            log_yellow("Instance state: %s" % instance.state)
            sleep(10)
            instance.update()


def down_rackspace():
    pass


def ec2():
    env.cloud = 'ec2'


def enable_firewalld_service():
    """ install and enables the firewalld service """
    yum_install(packages=['firewalld'])
    systemd(service='firewalld', unmask=True)


def get_container_id(container):
        result = sudo("docker ps -a | grep %s | awk '{print $1}'" % container)
        return result


def get_ec2_info(instance_id):
    """ queries EC2 for details about a particular instance_id
    """
    conn = connect_to_ec2()
    instance = conn.get_only_instances(
        filters={'instance_id': instance_id}
        )[0]

    data = {}
    data['public_dns_name'] = instance.public_dns_name
    data['id'] = instance.id
    data['ip_address'] = instance.ip_address
    data['architecture'] = instance.architecture
    data['state'] = instance.state
    try:
        volume = conn.get_all_volumes(
            filters={'attachment.instance-id': instance.id})[0].id
        data['volume'] = volume
    except:
        data['volume'] = ''
    return data


def get_image_id(image):
        result = sudo("docker images | grep %s | awk '{print $3}'" % image)
        return result


def get_ip_address_from_rackspace_server(server_id):
    """
    returns an ipaddress for a rackspace instance
    """
    import re
    nova = connect_to_rackspace()
    server = nova.servers.get(server_id)
    # the server was assigned IPv4 and IPv6 addresses, locate the IPv4 address
    ip_address = None
    for network in server.networks['public']:
        if re.match('\d+\.\d+\.\d+\.\d+', network):
            ip_address = network
            break

    # find out if we have an ip address
    if ip_address is None:
        log_red('No IP address assigned')
        return False
    else:
        return ip_address


def get_rackspace_info(server_id):
    """ queries Rackspace for details about a particular server id
    """
    nova = connect_to_rackspace()
    server = nova.servers.get(server_id)

    data = {}
    data['id'] = server.id
    # this needs to be tackled
    data['ip_address'] = get_ip_address_from_rackspace_server(server.id)
    data['state'] = server.status

    # TODO: volumes are missing
    return data


def git_clone(repo_url, repo_name):
    from fabric.api import run
    from fabric.contrib.files import exists
    """ clones a git repository """
    if not exists(repo_name):
        run("git clone %s" % repo_url)


def halt():
    if is_there_state():
        if env.cloud == 'ec2':
            down_ec2()
        if env.cloud == 'rackspace':
            down_rackspace()


def install_docker():
    """ installs docker """
    yum_install(packages=['docker', 'docker-registry'])
    systemd('docker.service')


def install_gem(gem):
    """ install a particular gem """
    from fabric.api import settings, run
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=False, capture=True):
        run("gem install %s --no-rdoc --no-ri" % gem)


def install_recent_git_from_source():
    from fabric.context_managers import cd
    # update git
    sudo("wget -c https://www.kernel.org/pub/software/scm/git/git-2.4.6.tar.gz")
    sudo("test -e git-2.4.6 || tar -zxf git-2.4.6.tar.gz")
    with cd('git-2.4.6'):
        sudo('test -e /usr/local/bin/git || ./configure --prefix=/usr/local')
        sudo('test -e /usr/local/bin/git || make')
        sudo('test -e /usr/local/bin/git || make install')


def install_os_updates():
    """ installs OS updates """
    if ('centos' in linux_distribution() or
            'rhel' in linux_distribution() or
            'redhat' in linux_distribution()):
        sudo("yum -y --quiet update")

    if ('ubuntu' in linux_distribution() or
            'debian' in linux_distribution()):
        sudo("apt-get update")
        sudo("apt-get -y upgrade")


def install_python_module(name):
    """ instals a python module using pip """
    from fabric.api import settings, run
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=False, capture=True):
        run('pip --quiet install %s' % name)


def install_python_module_locally(name):
    """ instals a python module using pip """
    from fabric.api import settings, local
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=False, capture=True):
        local('pip --quiet install %s' % name)


def install_system_gem(gem):
    """ install a particular gem """
    from fabric.api import settings
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=False, capture=True):
        sudo("gem install %s --no-rdoc --no-ri" % gem)


def install_zfs_from_testing_repository():
    # Enable debugging for ZFS modules
    sudo("echo SPL_DKMS_DISABLE_STRIP=y >> /etc/sysconfig/spl")
    sudo("echo ZFS_DKMS_DISABLE_STRIP=y >> /etc/sysconfig/zfs")
    sudo("yum install --quiet -y --enablerepo=zfs-testing zfs")


def is_deb_package_installed(pkg):
    """ checks if a particular deb package is installed """

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=True, capture=True):

        result = sudo("dpkg -l %s" % pkg)
        if result.return_code == 0:
            return True
        elif result.return_code == 1:
            return False
        else:  # print error to user
            print(result)
            raise SystemExit()


def is_package_installed(pkg):
    """ checks if a particular package is installed """
    if ('centos' in linux_distribution() or
            'rhel' in linux_distribution() or
            'redhat' in linux_distribution()):
        return(is_rpm_package_installed(pkg))

    if ('ubuntu' in linux_distribution() or
            'debian' in linux_distribution()):
        return(is_deb_package_installed(pkg))


def is_rpm_package_installed(pkg):
    """ checks if a particular rpm package is installed """

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=True, capture=True):

        result = sudo("rpm -q %s" % pkg)
        if result.return_code == 0:
            return True
        elif result.return_code == 1:
            return False
        else:   # print error to user
            print(result)
            raise SystemExit()


def is_there_state():
    """ checks is there is valid state available on disk """
    if os.path.isfile('data.json'):
        return True
    else:
        return False


def is_ssh_available(host, port=22):
    """ checks if ssh port is open """
    s = socket.socket()
    try:
        s.connect((host, port))
        return True
    except socket.error:
        return False


def linux_distribution():
    """ returns the linux distribution in lower case """
    from fabric.api import run, settings
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=True, capture=True):
        if 'centos' in run('cat /etc/os-release').lower():
            return('centos')


def load_state_from_disk():
    """ loads the state from a loca data.json file
    """
    if is_there_state():
        with open('data.json', 'r') as f:
            data = json.load(f)
        return data
    else:
        return False


def log_green(msg):
    print(green(msg))


def log_yellow(msg):
    print(yellow(msg))


def log_red(msg):
    print(red(msg))


def print_ec2_info():
    """ outputs information about our EC2 instance """
    _state = load_state_from_disk()
    if _state:
        data = get_ec2_info(_state['id'])
        log_green("Instance state: %s" % data['state'])
        log_green("Public dns: %s" % data['public_dns_name'])
        log_green("Ip address: %s" % data['ip_address'])
        log_green("volume: %s" % data['volume'])
        log_green("user: %s" % env.user)
        log_green("ssh -i %s %s@%s" % (env.key_filename,
                                       env.user,
                                       data['ip_address']))


def print_rackspace_info():
    """ outputs information about our Rackspace instance """
    _state = load_state_from_disk()
    if _state:
        data = get_rackspace_info(_state['id'])
        log_green("Instance state: %s" % data['status'])
        log_green("Ip address: %s" % data['ip_address'])
        log_green("user: %s" % env.user)
        log_green("ssh -i %s %s@%s" % (env.key_filename,
                                       env.user,
                                       data['ip_address']))


def rackspace():
    env.cloud = 'rackspace'


def reboot():
    sudo('shutdown -r now')


def remove_image(image):
    sudo('docker rmi -f %s' % get_image_id(image))


def remove_container(container):
    sudo('docker rm -f %s' % get_container_id(container))


def rsync():
    """ syncs the src code to the remote box """
    from fabric.context_managers import lcd
    log_green('syncing code to remote box...')
    data = load_state_from_disk()
    if 'SOURCE_PATH' in os.environ:
        with lcd(os.environ['SOURCE_PATH']):
            local("rsync  -a "
                  "--info=progress2 "
                  "--exclude .git "
                  "--exclude .tox "
                  "--exclude .vagrant "
                  "--exclude venv "
                  ". "
                  "-e 'ssh -C -i " + env.ec2_key_filename + "' "
                  "%s@%s:" % (env.user, data['ip_address']))
    else:
        print('please export SOURCE_PATH before running rsync')
        exit(1)


def save_state_locally(instance_id):
    """ queries EC2 for details about a particular instance_id and
        stores those details locally
    """
    if env.cloud == 'ec2':
        data = get_ec2_info(instance_id)
        data['cloud_type'] = 'ec2'
    if env.cloud == 'rackspace':
        data = get_rackspace_info(instance_id)
        data['cloud_type'] = 'rackspace'

    with open('data.json', 'w') as f:
        json.dump(data, f)


def sleep_for_one_minute():
    from time import sleep
    sleep(60)


def ssh_session(*cli):
    from itertools import chain
    """ opens a ssh shell to the host """
    data = load_state_from_disk()
    local('ssh -t -i %s %s@%s %s' % (env['ec2_key_filename'],
                                     env['user'], data['ip_address'],
                                     "".join(chain.from_iterable(cli))))


def status():
    if is_there_state():
        if env.cloud == 'ec2':
            print_ec2_info()
        if env.cloud == 'rackspace':
            print_rackspace_info()


def systemd(service, start=True, enabled=True, unmask=False):
    """ manipulates systemd services """
    from fabric.api import settings
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=True, capture=True):

        if start:
            sudo('systemctl start %s' % service)
        else:
            sudo('systemctl stop %s' % service)

        if enabled:
            sudo('systemctl enable %s' % service)
        else:
            sudo('systemctl disable %s' % service)

        if unmask:
            sudo('systemctl unmask %s' % service)


def terminate():
    destroy()


def up():
    if hasattr(env, 'cloud'):
        if env.cloud == 'ec2':
            up_ec2()
        if env.cloud == 'rackspace':
            up_rackspace()


def up_ec2():
    """ boots an existing ec2_instance, or creates a new one if needed """
    # if we don't have a state file, then its likely we need to create a new
    # ec2 instance.
    if is_there_state() is False:
        create_server_ec2()
    else:
        conn = connect_to_ec2()
        # there is a data.json file, which contains our ec2 instance_id
        data = load_state_from_disk()
        # boot the ec2 instance
        instance = conn.start_instances(instance_ids=[data['id']])[0]
        while instance.state != "running":
            log_yellow("Instance state: %s" % instance.state)
            sleep(10)
            instance.update()
        # the ip_address has changed so we need to get the latest data from ec2
        data = get_ec2_info(data['id'])
        # and make sure we don't return until the instance is fully up
        wait_for_ssh(data['ip_address'])
        # lets update our local state file with the new ip_address
        save_state_locally(instance.id)
        env.hosts = data['ip_address']
        print_ec2_info()


def up_rackspace():
    """ boots an existing rackspace instance, or creates a new one if needed """
    # if we don't have a state file, then its likely we need to create a new
    # rackspace instance.
    if is_there_state() is False:
        create_server_rackspace()
    else:
        nova = connect_to_rackspace()
        # there is a data.json file, which contains our server id
        data = load_state_from_disk()
        # boot the rackspace instance
        # rackspace doesn't provide us with a 'up' method, it expects us
        # to use reboot to power up the server
        server = nova.servers.get(data['id'])
        if server.status != "ACTIVE":
            server.reboot('hard')
            wait_for_ssh(data['ip_address'])
            save_state_locally(server.id)
            print_rackspace_info()


def update_system_pip_to_latest_pip():
    """ install the latest pip """
    from fabric.api import settings
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=False, capture=True):
        sudo("pip install --quiet --upgrade pip")


def update_to_latest_pip():
    """ install the latest pip """
    from fabric.api import settings, run
    from fabric.context_managers import hide

    with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                  warn_only=False, capture=True):
        run("pip install --quiet --upgrade pip")


def yum_install(**kwargs):
    """
        installs a yum package
    """
    if 'repo' in kwargs:
        repo = kwargs['repo']

    for pkg in list(kwargs['packages']):
        if is_package_installed(pkg) is False:
            if 'repo' in locals():
                log_green("installing %s from repo %s ..." % (pkg, repo))
                sudo("yum install -y --quiet --enablerepo=%s %s" % (repo, pkg))
            else:
                log_green("installing %s ..." % pkg)
                sudo("yum install -y --quiet %s" % pkg)


def yum_install_from_url(pkg_name, url):
    """ installs a pkg from a url
        p pkg_name: the name of the package to install
        p url: the full URL for the rpm package
    """

    from fabric.api import settings
    from fabric.context_managers import hide

    if is_package_installed(pkg_name) is False:
        log_green("installing %s from %s" % (pkg_name, url))
        with settings(hide('warnings', 'running', 'stdout', 'stderr'),
                      warn_only=True, capture=True):

            result = sudo("rpm -i %s" % url)
            if result.return_code == 0:
                return True
            elif result.return_code == 1:
                return False
            else:  # print error to user
                print(result)
                raise SystemExit()


def wait_for_ssh(host, port=22, timeout=600):
    """ probes the ssh port and waits until it is available """
    log_yellow('waiting for ssh...')
    for iteration in xrange(1, timeout):
        if is_ssh_available(host, port):
            log_green('ssh is now available.')
            return True
        else:
            log_yellow('waiting for ssh...')
        sleep(1)
