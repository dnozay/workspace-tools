from __future__ import absolute_import
import logging
import os

import click

from workspace.commands import AbstractCommand
from workspace.commands.helpers import expand_product_groups
from workspace.scm import (checkout_product, checkout_branch, all_branches, checkout_files, is_repo,
                           product_checkout_path, product_name)
log = logging.getLogger(__name__)


class Checkout(AbstractCommand):
    """
      Checkout products (repo urls) or branch, or revert files.

      :param list target: List of products (git repository URLs) to checkout. When inside a git repo,
                          checkout the branch or revert changes for file(s).
    """
    alias = 'co'

    @classmethod
    def arguments(cls):
        _, docs = cls.docs()
        return [cls.make_args('target', nargs='+', help=docs['target'])]

    def run(self):
        if is_repo():
            if len(self.target) == 1 and self.target[0] in all_branches():
                checkout_branch(self.target[0])
                click.echo('Switched to branch ' + self.target[0])
            elif len(self.target) == 1 and 'remotes/{}'.format(self.target[0]) in all_branches(remotes=True) and '/' in self.target[0]:
                name = self.target[0].split('/')[-1]
                checkout_branch(self.target[0], name=name)
                click.echo('Switched to branch ' + name)
            else:
                checkout_files(self.target)
            return

        product_urls = expand_product_groups(self.target)

        for product_url in product_urls:
            product_url = product_url.strip('/')

            product_path = product_checkout_path(product_url)

            if os.path.exists(product_path):
                click.echo('Updating ' + product_name(product_path))
            else:
                click.echo('Checking out ' + product_url)

            checkout_product(product_url, product_path)
