import argparse
from glob import glob
import logging
import os
import re
import sys

from localconfig import LocalConfig
from workspace.config import product_groups, USER_CONFIG_FILE

from workspace.commands.checkout import checkout
from workspace.commands.helpers import expand_product_groups
from workspace.commands.test import test
from workspace.scm import is_repo, repo_check, product_name, repo_path, product_path
from workspace.utils import split_doc, run


log = logging.getLogger(__name__)

BASHRC_FILE = "~/.bashrc"
WSTRC_FILE = "~/.wstrc"

WS_SETUP_START = '# Added by "workspace setup" (do not remove comments before / after function)'
WS_SETUP_END = "# workspace setup - end"

WS_FUNCTION_TEMPLATE = """\
alias _wst=%s

function ws() {
  if [ $# -gt 0 ]; then
    _wst "$@";
  else
    cd %s
    ls
  fi
}

function activate() {
  if [ -e activate ]; then
    source activate
  elif [ -e ${PWD##*/}/activate ]; then
    source ${PWD##*/}/activate
  else
    source .tox/${PWD##*/}/bin/activate
  fi
}

function open_files_from_last_command() {
  last_command=`history 100 | grep  -E "^\s+[0-9]+\s+(ag|find|which) " | tail -1`

  if [ -z "$last_command" ]; then
    echo No ag or find command found in last 100 commands.
    return
  fi

  declare -a "parts=($last_command)"
  command=${parts[1]}

  if [[ $command == "ag" ]]; then
    full_command=${parts[@]:1}
    pattern=+/${parts[2]}

    raw_parts=(${last_command// / })  # Need the quote retained to sub properly
    last_part=${raw_parts[@]:(-1)}

    if [[ $last_part != "-l" ]]; then
        sub_expr="$last_part=$last_part -l"
    else
        sub_expr=
    fi

    if [ -z "$sub_expr" ]; then
      files=`fc -s $command`
    else
      files=`fc -s "$sub_expr" $command`
    fi

    if [ -z "$files" ]; then
      echo No files found from output
    else
      vim -p $files "$pattern" --cmd "set ignorecase smartcase"
    fi

  else
    files=`fc -s $command`

    if [ -z "$files" ]; then
      echo No files found from output
    else
      vim -p $files
    fi

  fi
}
"""
COMMAND_FUNCTION_TEMPLATE = 'function %s() { _wst %s "$@"; }\n'
COMMAND_ALIAS_TEMPLATE = 'alias %s=%s\n'
COMMANDS = {
  'a': "'activate'",
  'd': "'deactivate'",
  'tv': "'open_files_from_last_command'  # from ag/find/which [t]o [v]im",

  'co': 'checkout',
  'ci': 'commit',
  'di': '_diff',
  'st': 'status',
  'up': 'update',

  '_bu': 'bump',
  '_cl': 'clean',
  '_lo': 'log',
  '_pu': 'push',
  '_pb': 'publish',
  '_te': 'test',
}
AUTO_COMPLETE_TEMPLATE = """
function _branch_file_completer() {
  local cur=${COMP_WORDS[COMP_CWORD]}

  if git status &> /dev/null; then
    branches=`git branch`
  else
    branches=
  fi

  if [ ! -z "$branches" ]; then
    COMPREPLY=( $( compgen -W "$branches" -- $cur ) )
  fi
}
function _env_file_completer() {
  local cur=${COMP_WORDS[COMP_CWORD]}

  if ls tox*.ini &>/dev/null || ls .tox*.ini &>/dev/null; then
    envs=`grep -h '^\[testenv:' .tox*.ini tox*.ini 2>/dev/null | sed -E 's/^\[testenv:(.+)]/\\1/' | grep -vE '^(py|pydev)$'`
    COMPREPLY=( $( compgen -W "$envs" -- $cur ) )
  fi
}

complete -o default -F _branch_file_completer co
complete -o default -F _branch_file_completer checkout
complete -o default -F _env_file_completer test
complete -F _branch_file_completer push

complete -o default log
complete -o default di
"""
TOX_INI_FILE = 'tox.ini'
TOX_INI_TMPL = """\
[tox]
envlist = py27

[testenv]
commands =
    py.test {env:PYTESTARGS:}
install_command = pip install -U {packages} --download-cache={[testenv]downloadcache}
downloadcache = {toxworkdir}/_download
recreate = False
skipsdist = True
usedevelop = True
setenv =
    PIP_PROCESS_DEPENDENCY_LINKS=1
    PIP_DEFAULT_TIMEOUT=60
    ARCHFLAGS=-Wno-error=unused-command-line-argument-hard-error-in-future
basepython = python

[testenv:py]
deps =
    pytest
    pytest-xdist

[testenv:pydev]
deps =
    {[testenv:py]deps}
    {[testenv:style]deps}
    pytest-cov
    sphinx!=1.2b2

[testenv:py27]
deps = {[testenv:pydev]deps}
envdir = {toxworkdir}/%s
basepython = python2.7

[testenv:style]
commands =
    flake8 --config tox.ini src test
deps =
    flake8

[testenv:coverage]
commands =
    py.test {env:PYTESTARGS:} --cov=src --cov-report=xml --cov-report=html --cov-report=term test
deps =
    {[testenv:py]deps}
    pytest-cov

[flake8]
ignore = E111,E121,W292,E123,E226
max-line-length = 160

[pytest]
addopts = -n 4

[wst]
template_version = 1
"""
SETUP_PY_TMPL = """\
#!/usr/bin/env python

import os
import setuptools


setuptools.setup(
  name='%s',
  version='0.0.1',

  author='<PLACEHOLDER>',
  author_email='<PLACEHOLDER>',

  description='<PLACEHOLDER>',
  long_description=open('%s').read(),

  url='<PLACEHOLDER>',

#  entry_points={
#    'console_scripts': [
#      'script_name = package.module:entry_callable',
#    ],
#  },

  install_requires=open('%s').read(),

  license='MIT',

  package_dir={'': 'src'},
  packages=setuptools.find_packages('src'),
  include_package_data=True,

  setup_requires=['setuptools-git'],

#  scripts=['bin/cast-example'],

  classifiers=[
    'Development Status :: 5 - Production/Stable',

    'Intended Audience :: Developers',
    'Topic :: Software Development :: <PLACEHOLDER SUB-TOPIC>',

    'License :: OSI Approved :: MIT License',

    'Programming Language :: Python :: 2',
    'Programming Language :: Python :: 2.6',
    'Programming Language :: Python :: 2.7',
  ],

  keywords='<KEYWORDS>',
)
"""
README_TMPL = """\
%s
===========

<PLACEHOLDER DESCRIPTION>
"""


def setup_setup_parser(subparsers):
  doc, docs = split_doc(setup.__doc__)
  setup_parser = subparsers.add_parser('setup', description=doc, formatter_class=argparse.RawDescriptionHelpFormatter,
                                       help='Setup workspace or product environment')
  group = setup_parser.add_mutually_exclusive_group(required=True)
  group.add_argument('product_group', nargs='?', help=docs['product_group'])
  group.add_argument('--product', action='store_true', help=docs['product'])
  group.add_argument('--commands', action='store_true', help=docs['commands'])
  group.add_argument('-a', '--commands-with-aliases', action='store_true', help=docs['commands_with_aliases'])
  group.add_argument('--uninstall', action='store_true', help=docs['uninstall'])
  setup_parser.set_defaults(command=setup)

  return setup_parser


def setup(product_group=None, product=False, commands=None, commands_with_aliases=None, uninstall=False,
          additional_commands=None, checkout_product=None, test_product=None, **kwargs):
  """
  Sets up workspace or product environment.

  :param str product_group: Setup product group by checking them out, developing them, and running any setup scripts and
                            exports as defined by setup.cfg in each product.
  :param bool product: Initialize product by setting up tox with py27, style, and coverage test environments.
                       Also create setup.py, README.rst, and src / test directories if they don't exist.
  :param bool commands: Add convenience bash function for certain commands, such as checkout to run
                        "workspace checkout", or "ws" bash function that goes to your workspace directory
                        when no argument is passed in, otherwise runs wst command.
  :param bool commands_with_aliases: Same as --commands plus add shortcut aliases, like "co" for checkout.
                                     This is for those developers that want to get as much done with the least
                                     key strokes - true efficienist! ;)
  :param callable checkout_product: Callable to checkout a list of product or product group
  :param callable test_product: Callable to test a product
  :param bool uninstall: Uninstall all functions/aliases.
  """
  if product_group:
    setup_product_group(product_group, checkout_product, test_product)
  elif product:
    setup_product()
  else:
    setup_workspace(commands, commands_with_aliases, uninstall, additional_commands)


def setup_product_group(group, checkout_product=None, test_product=None):
  if is_repo():
    log.error('This should be run from your workspace directory and not within a product repo')
    sys.exit(1)

  if group not in product_groups():
    log.error('Product group "%s" is not defined in workspace.cfg', group)
    sys.exit(1)

  if not checkout_product:
    checkout_product = checkout

  log.info('Setting up %s products', group)

  # Checkout product
  checkout_product([group])

  # Add to editable_products
  user_config = LocalConfig(USER_CONFIG_FILE, compact_form=True)
  not_set = not user_config.get('test', 'editable_products', None)
  if not_set or group not in user_config.test.editable_products:
    if not_set:
      if 'test' not in user_config:
        user_config.add_section('test')
      user_config.test.editable_products = group
    else:
      products = user_config.test.editable_products.split()
      products.append(group)
      user_config.test.editable_products = ' '.join(sorted(products))

    user_config.save()
    log.info('Added "%s" to editable_products in %s', group, USER_CONFIG_FILE)

  # Develop the environment
  if not test_product:
    test_product = test
  current_dir = os.getcwd()

  for product in expand_product_groups([group]):
    log.info('Developing environment for %s', product)
    try:
      repo = product_path(product)
      os.chdir(repo)
      test_product(redevelop=True, install_only=True)

    except Exception as e:
      log.error('Error occurred when developing %s: %s', product, e)

    finally:
      os.chdir(current_dir)

  # Process setup.cfg
  exports = {}
  products = expand_product_groups([group])
  for product in products:
    repo = product_path(product)

    if os.path.join(repo, product, 'setup.cfg'):
      setup_cfg = os.path.join(repo, product, 'setup.cfg')
    else:
      setup_cfg = os.path.join(repo, 'setup.cfg')

    if os.path.exists(setup_cfg):
      setup = LocalConfig(setup_cfg)
      setup._parser.optionxform = str
      if 'scripts' in setup or 'exports' in setup:
        log.info('Processing scripts/exports in setup.cfg for %s', product)

        if 'scripts' in setup:
          cwd = os.getcwd()
          try:
            os.chdir(repo)
            for name, script in setup.scripts:
              log.info('Running %s', name)
              run(['bash', '-c', '; '.join(filter(None, script.split('\n')))])
          except Exception as e:
            log.error('Error occurred running script: %s', e)
          finally:
            os.chdir(cwd)

        if 'exports' in setup:
          for name, value in setup.exports:
            exports[name] = value

  if exports:
    activate_group = 'activate_%s' % group
    with open(activate_group, 'w') as fp:
      for name in sorted(exports):
        fp.write('export %s=%s\n' % (name, exports[name]))
      fp.write('\n')

      fp.write('if [[ $PS1 != *{%s}* ]]; then\n' % group)
      fp.write('  export PS1="{%s}$PS1"\n' % group)
      fp.write('fi\n\n')

      fp.write('deactivate_%s() {\n' % group)
      for name in sorted(exports):
        fp.write('  unset %s\n' % name)
      fp.write('\n')
      fp.write('  export PS1=${PS1/{%s\}/}\n' % group)
      fp.write('  unset deactivate_%s\n' % group)
      fp.write('}\n')
    log.info('Created ./%s. To activate, run: source %s. To deactivate, run: deactivate_%s', activate_group, activate_group, group)


def setup_product():
  repo_check()

  name = product_name(repo_path())
  placeholder_info = '- please update <PLACEHOLDER> with appropriate value'

  tox_ini = TOX_INI_TMPL % name
  tox_ini_file = os.path.join(repo_path(), TOX_INI_FILE)
  tox_change_word = 'Updated' if os.path.exists(tox_ini_file) else 'Created'
  with open(tox_ini_file, 'w') as fp:
    fp.write(tox_ini)

  log.info('%s %s', tox_change_word, _relative_path(tox_ini_file))

  readme_files = glob(os.path.join(repo_path(), 'README*'))
  if readme_files:
    readme_file = readme_files[0]
  else:
    readme_file = os.path.join(repo_path(), 'README.rst')
    with open(readme_file, 'w') as fp:
      fp.write(README_TMPL % name)
    log.info('Created %s %s', _relative_path(readme_file), placeholder_info)

  setup_py_file = os.path.join(repo_path(), 'setup.py')
  if not os.path.exists(setup_py_file):
    requirements_file = os.path.join(repo_path(), 'requirements.txt')
    if not os.path.exists(requirements_file):
      with open(requirements_file, 'w') as fp:
        pass
      log.info('Created %s', _relative_path(requirements_file))

    readme_name = os.path.basename(readme_file)
    requirements_name = os.path.basename(requirements_file)

    with open(setup_py_file, 'w') as fp:
      fp.write(SETUP_PY_TMPL % (name, readme_name, requirements_name))

    log.info('Created %s %s', _relative_path(setup_py_file), placeholder_info)

  src_dir = os.path.join(repo_path(), 'src')
  if not os.path.exists(src_dir):
    package_dir = os.path.join(src_dir, re.sub('[^A-Za-z]', '', name))
    os.makedirs(package_dir)
    init_file = os.path.join(package_dir, '__init__.py')
    open(init_file, 'w').close()
    log.info('Created %s', _relative_path(init_file))

  test_dir = os.path.join(repo_path(), 'test')
  if not os.path.exists(test_dir):
    os.makedirs(test_dir)
    test_file = os.path.join(test_dir, 'test_%s.py' % re.sub('[^A-Za-z]', '_', name))
    with open(test_file, 'w') as fp:
      fp.write('# Placeholder for tests')
    log.info('Created %s', _relative_path(test_file))


def setup_workspace(commands, commands_with_aliases, uninstall, additional_commands):
  bashrc_content = None
  bashrc_path = os.path.expanduser(BASHRC_FILE)
  wstrc_path = os.path.expanduser(WSTRC_FILE)

  bashrc_script = []

  if os.path.exists(bashrc_path):
    with open(bashrc_path) as fh:
      bashrc_content = fh.read()

    skip = False
    for line in bashrc_content.split('\n'):
      if line in (WS_SETUP_START, WS_SETUP_END):
        skip = not skip
        continue
      if not skip and WSTRC_FILE not in line:
        bashrc_script.append(line)

    bashrc_script = '\n'.join(bashrc_script).strip().split('\n')  # could be better

  repo_path = is_repo()
  if repo_path:
    workspace_dir = os.path.dirname(repo_path).replace(os.path.expanduser('~'), '~')
  else:
    workspace_dir = os.getcwd().replace(os.path.expanduser('~'), '~')

  with open(bashrc_path, 'w') as fh:
    if bashrc_script:
      fh.write('\n'.join(bashrc_script) + '\n\n')

    if uninstall:
      if os.path.exists(wstrc_path):
        os.unlink(wstrc_path)
      log.info('Removed %s and its sourcing reference from %s', WSTRC_FILE, BASHRC_FILE)
      log.info('Please restart your bash session for the change to take effect')
      return

    fh.write('source %s\n' % WSTRC_FILE)

  with open(wstrc_path, 'w') as fh:
    fh.write(WS_FUNCTION_TEMPLATE % (os.path.realpath(sys.argv[0]), workspace_dir))
    log.info('Added "ws" bash function with workspace directory set to %s', workspace_dir)

    if additional_commands:
      COMMANDS.update(additional_commands)

    special = lambda c: c.startswith("'") or c.startswith('"') or c.startswith(' ')

    if commands or commands_with_aliases:
      functions = sorted([f for f in COMMANDS.values() if not special(f)])
      fh.write('\n')
      for func in functions:
        fh.write(COMMAND_FUNCTION_TEMPLATE % (func, func.lstrip('_')))
      log.info('Added bash functions: %s', ', '.join([f for f in functions if not f.startswith('_')]))

    if commands_with_aliases:
      fh.write('\n')
      aliases = [item for item in sorted(COMMANDS.items(), key=lambda x: x[1].lstrip('_')) if not item[0].startswith('_')]
      for alias, command in aliases:
        fh.write(COMMAND_ALIAS_TEMPLATE % (alias, command.lstrip(' ')))
      log.info('Added aliases: %s', ', '.join(["%s=%s" % (a, c.lstrip('_ ')) for a, c in aliases if not special(c)]))
      log.info('Added special aliases: %s', ', '.join(["%s=%s" % (a, c.lstrip('_ ')) for a, c in aliases if special(c)]))

      fh.write(AUTO_COMPLETE_TEMPLATE)

  log.info('To use, run "source %s" or open a new shell.', WSTRC_FILE)


def _relative_path(path):
  if path.startswith(os.getcwd() + os.path.sep):
    path = path[len(os.getcwd())+1:]
  return path
