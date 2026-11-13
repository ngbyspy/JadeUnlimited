import unittest
from unittest.mock import Mock, patch

from jade_unlimited.app import Application
from jade_unlimited.engine import Settings
from jade_unlimited.windows import InputNotSent


class AppPermissionTests(unittest.TestCase):
    def test_permission_rejection_prevents_worker_creation(self):
        app = Application.__new__(Application)
        app.worker = None
        app.armed = True
        app.settings = Mock(return_value=Settings(123, 100, 45))
        app.status = Mock()
        app.log = Mock()
        app.root = Mock()
        with patch("jade_unlimited.app.ensure_input_access", side_effect=InputNotSent("need administrator")), patch("jade_unlimited.app.Worker") as worker, patch("jade_unlimited.app.messagebox.showerror"):
            app.toggle()
        worker.assert_not_called()
        self.assertFalse(app.armed)
        app.status.set.assert_called_once_with("need administrator")

    def test_cancelled_elevation_keeps_current_app_open(self):
        app = Application.__new__(Application)
        app.worker = None
        app.status = Mock()
        app.log = Mock()
        app.close = Mock()
        with patch("jade_unlimited.app.relaunch_as_administrator", side_effect=RuntimeError("cancelled")):
            app.restart_as_administrator()
        app.close.assert_not_called()
        app.status.set.assert_called_once_with("cancelled")


if __name__ == "__main__":
    unittest.main()
