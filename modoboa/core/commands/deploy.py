# coding: utf-8

"""A shortcut to deploy a fresh modoboa instance."""

import getpass
import os
import shutil
import subprocess
import sys

try:
    import pip
except ImportError:
    sys.stderr.write("Error: pip is required to install extensions.\n")
    sys.exit(2)

import django
from django.core import management
from django.template import Context, Template

import dj_database_url

from modoboa.core.commands import Command
from modoboa.lib.api_client import ModoAPIClient

DBCONN_TPL = """
    '{{ conn_name }}': {
        'ENGINE': '{{ ENGINE }}',
        'NAME': '{{ NAME }}',
        'USER': '{% if USER %}{{ USER }}{% endif %}',
        'PASSWORD': '{% if PASSWORD %}{{ PASSWORD }}{% endif %}',
        'HOST': '{% if HOST %}{{ HOST }}{% endif %}',
        'PORT': '{% if PORT %}{{ PORT }}{% endif %}',
        'ATOMIC_REQUESTS': True,
        {% if ENGINE == 'django.db.backends.mysql' %}'OPTIONS' : {
            "init_command" : 'SET foreign_key_checks = 0;',
        },{% endif %}
    },
"""


class DeployCommand(Command):

    """The ``deploy`` command."""

    help = (
        "Create a fresh django project (calling startproject)"
        " and apply Modoboa specific settings."
    )

    def __init__(self, *args, **kwargs):
        super(DeployCommand, self).__init__(*args, **kwargs)
        self._parser.add_argument('name', type=str,
                                  help='The name of your Modoboa instance')
        self._parser.add_argument(
            '--collectstatic', action='store_true', default=False,
            help='Run django collectstatic command'
        )
        self._parser.add_argument(
            '--dburl', type=str, nargs="+", default=None,
            help='A database-url with a name')
        self._parser.add_argument(
            '--domain', type=str, default=None,
            help='The domain under which you want to deploy modoboa')
        self._parser.add_argument(
            '--lang', type=str, default="en-us",
            help="Set the default language"
        )
        self._parser.add_argument(
            '--timezone', type=str, default="UTC",
            help="Set the local timezone"
        )
        self._parser.add_argument(
            '--devel', action='store_true', default=False,
            help='Create a development instance'
        )
        self._parser.add_argument(
            '--extensions', type=str, nargs='*',
            help="The list of extension to deploy"
        )
        self._parser.add_argument(
            '--dont-install-extensions', action='store_true', default=False,
            help='Do not install extensions using pip'
        )

    def _exec_django_command(self, name, cwd, *args):
        """Run a django command for the freshly created project

        :param name: the command name
        :param cwd: the directory where the command must be executed
        """
        cmd = 'python manage.py %s %s' % (name, " ".join(args))
        if not self._verbose:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                shell=True, cwd=cwd
            )
            output = p.communicate()
        else:
            p = subprocess.Popen(cmd, shell=True, cwd=cwd)
            p.wait()
            output = None
        if p.returncode:
            if output:
                print >> sys.stderr, "\n".join(
                    [l for l in output if l is not None])
            print >> sys.stderr, "%s failed, check your configuration" % cmd

    def ask_db_info(self, name='default'):
        """Prompt the user for database information

        Gather all information required to create a new database
        connection (into settings.py).

        :param name: the connection name
        """
        print "Configuring database connection: %s" % name
        info = {
            'conn_name': name,
            'ENGINE': raw_input('Database type (mysql, postgres or sqlite3): ')
        }
        if info['ENGINE'] not in ['mysql', 'postgres', 'sqlite3']:
            raise RuntimeError('Unsupported database engine')

        if info['ENGINE'] == 'sqlite3':
            info['ENGINE'] = 'django.db.backends.sqlite3'
            info['NAME'] = '%s.db' % name
            return info
        if info['ENGINE'] == 'postgres':
            info['ENGINE'] = 'django.db.backends.postgresql_psycopg2'
            default_port = 5432
        else:
            info['ENGINE'] = 'django.db.backends.mysql'
            default_port = 3306
        info['HOST'] = raw_input("Database host (default: 'localhost'): ")
        info['PORT'] = raw_input(
            "Database port (default: '%s'): " % default_port)
        # leave port setting empty, if default value is supplied and
        # leave it to django
        if info['PORT'] == default_port:
            info['PORT'] = ''
        info['NAME'] = raw_input('Database name: ')
        info['USER'] = raw_input('Username: ')
        info['PASSWORD'] = getpass.getpass('Password: ')
        return info

    def _get_extension_list(self):
        """Ask the API to get the list of all extensions.

        We hardcode the API url here to avoid a loading of
        django's settings since they are not available yet...
        """
        url = "http://api.modoboa.org/"
        official_exts = ModoAPIClient(url).list_extensions()
        return [extension["name"] for extension in official_exts]

    def install_extensions(self, extensions):
        """Install one or more extensions.

        Return the list of extensions providing settings we must
        include in the final configuration.

        """
        pip_args = ["install"] + [extension[0] for extension in extensions]
        pip.main(pip_args)
        extra_settings = []
        for extension in extensions:
            module = __import__(extension[1], locals(), globals(), [])
            basedir = os.path.dirname(module.__file__)
            if not os.path.exists("{0}/settings.py".format(basedir)):
                continue
            extra_settings.append(extension[1])
        return extra_settings

    def handle(self, parsed_args):
        django.setup()
        management.call_command(
            'startproject', parsed_args.name, verbosity=False
        )
        path = "%(name)s/%(name)s" % {'name': parsed_args.name}
        sys.path.append(parsed_args.name)

        conn_tpl = Template(DBCONN_TPL)
        connections = {}
        if parsed_args.dburl:
            for dburl in parsed_args.dburl:
                conn_name, url = dburl.split(":", 1)
                info = dj_database_url.config(default=url)
                # In case the user fails to supply a valid database url,
                # fallback to manual mode
                if not info:
                    print "There was a problem with your database-url. \n"
                    info = self.ask_db_info(conn_name)
                # If we set this earlier, our fallback method will never
                # be triggered
                info['conn_name'] = conn_name
                connections[conn_name] = conn_tpl.render(Context(info))
        else:
            connections["default"] = conn_tpl.render(
                Context(self.ask_db_info()))

        if parsed_args.domain:
            allowed_host = parsed_args.domain
        else:
            allowed_host = raw_input(
                'What will be the hostname used to access Modoboa? '
            )
            if not allowed_host:
                allowed_host = "localhost"
        extra_settings = []
        extensions = parsed_args.extensions
        if extensions:
            if "all" in extensions:
                extensions = self._get_extension_list()
            extensions = [(extension, extension.replace("-", "_"))
                          for extension in extensions]
            if not parsed_args.dont_install_extensions:
                extra_settings = self.install_extensions(extensions)
            extensions = [extension[1] for extension in extensions]

        bower_components_dir = os.path.realpath(
            os.path.join(os.path.dirname(__file__), "../../bower_components")
        )

        mod = __import__(parsed_args.name, globals(), locals(), ['settings'])
        tpl = self._render_template(
            "%s/settings.py.tpl" % self._templates_dir, {
                'db_connections': connections,
                'secret_key': mod.settings.SECRET_KEY,
                'name': parsed_args.name,
                'allowed_host': allowed_host,
                'lang': parsed_args.lang,
                'timezone': parsed_args.timezone,
                'bower_components_dir': bower_components_dir,
                'devmode': parsed_args.devel,
                'extensions': extensions,
                'extra_settings': extra_settings
            }
        )
        with open("%s/settings.py" % path, "w") as fp:
            fp.write(tpl)
        shutil.copyfile(
            "%s/urls.py.tpl" % self._templates_dir, "%s/urls.py" % path
        )
        os.mkdir("%s/media" % parsed_args.name)

        os.unlink("%s/settings.pyc" % path)
        self._exec_django_command(
            "migrate", parsed_args.name, '--noinput'
        )
        self._exec_django_command(
            "load_initial_data", parsed_args.name
        )
        if parsed_args.collectstatic:
            self._exec_django_command(
                "collectstatic", parsed_args.name, '--noinput'
            )
        self._exec_django_command(
            "set_default_site", parsed_args.name, allowed_host
        )
