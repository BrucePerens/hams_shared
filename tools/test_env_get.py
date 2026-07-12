# This software is distributed under the terms of the Affero General Public License (AGPL-3).

from odoo import api, SUPERUSER_ID
from odoo.modules.registry import Registry

def run_test():
    registry = Registry('postgres') # or any db
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        print(getattr(env, 'get', 'No get method'))

if __name__ == '__main__':
    run_test()
