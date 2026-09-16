"""在 PyCharm 中右键运行本文件，即可打开桌面界面。"""
import ctypes
import sys
import tkinter as tk

from ticket_app.gui import App


def main():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
