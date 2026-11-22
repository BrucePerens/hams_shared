# SPDX-License-Identifier: AGPL-3.0-or-later
"""check_res_config_settings_access._check_csv must not skip a file it cannot judge."""
import os
import tempfile
import unittest

import check_res_config_settings_access as check

HEADER = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"


class CheckCsvTests(unittest.TestCase):
    def _csv(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_a_grant_to_another_group_is_reported(self):
        path = self._csv(HEADER + "a,a,base.model_res_config_settings,base.group_user,1,1,1,0\n")
        self.assertEqual(len(check._check_csv("m", path)), 1)

    def test_a_system_grant_is_clean(self):
        path = self._csv(HEADER + "a,a,base.model_res_config_settings,base.group_system,1,1,1,0\n")
        self.assertEqual(check._check_csv("m", path), [])

    def test_the_slash_id_header_spelling_is_still_checked(self):
        path = self._csv("id,name,model_id/id,group_id/id,perm_read\na,a,base.model_res_config_settings,base.group_user,1\n")
        self.assertEqual(len(check._check_csv("m", path)), 1)

    def test_a_file_with_no_model_column_is_reported_not_skipped(self):
        path = self._csv("id,name,model,group\na,a,x,y\n")
        self.assertEqual(len(check._check_csv("m", path)), 1)

    def test_an_unreadable_file_is_reported_not_skipped(self):
        self.assertEqual(len(check._check_csv("m", "/nonexistent/ir.model.access.csv")), 1)


if __name__ == "__main__":
    unittest.main()
