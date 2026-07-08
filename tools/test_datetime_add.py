from odoo import fields

def run_test():
    print(hasattr(fields.Datetime, 'add'))

if __name__ == '__main__':
    run_test()
