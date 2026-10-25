import os
import unittest
import uuid
from unittest import mock

import window_activation


@unittest.skipUnless(os.name == "nt", "Windows window activation")
class ActivationTests(unittest.TestCase):
    def test_real_tk_window_is_restored_from_minimized(self):
        import tkinter as tk
        root = tk.Tk()
        root.attributes("-alpha", 0)
        title = "Beyond-SLAM activation-test " + uuid.uuid4().hex
        root.title(title)
        try:
            root.update()
            root.iconify()
            root.after(100, root.quit)
            root.mainloop()
            self.assertEqual(root.state(), "iconic")
            self.assertTrue(window_activation.activate_existing_window(prefix=title))
            root.update()
            self.assertEqual(root.state(), "normal")
        finally:
            root.destroy()

    def test_second_launch_restores_only_the_matching_tk_app(self):
        api = mock.Mock()
        titles = {1: ("Chrome_WidgetWin_1", "Beyond-SLAM example"), 2: ("TkTopLevel", "Other app"), 3: ("TkTopLevel", "Beyond-SLAM 0.4.0")}
        def enumerate_windows(callback, context):
            for handle in titles:
                if not callback(handle, context):
                    break
        api.EnumWindows.side_effect = enumerate_windows
        api.GetClassNameW.side_effect = lambda handle, buffer, size: setattr(buffer, "value", titles[handle][0]) or 1
        api.GetWindowTextW.side_effect = lambda handle, buffer, size: setattr(buffer, "value", titles[handle][1]) or 1
        api.IsIconic.return_value = True
        self.assertTrue(window_activation.activate_existing_window(user32=api))
        api.ShowWindow.assert_called_once_with(3, 9)
        api.SetForegroundWindow.assert_called_once_with(3)
