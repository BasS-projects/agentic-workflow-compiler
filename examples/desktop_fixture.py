"""Disposable real Tk window for Xvfb desktop SIT; no network or user data.

Run: DISPLAY=:99 python examples/desktop_fixture.py /tmp/desktop-receipt.txt
"""

from pathlib import Path
import sys
import tkinter as tk


def main():
    target = Path(sys.argv[1])
    root = tk.Tk()
    root.title("Workflow Desktop SIT")
    root.geometry("480x240+0+0")
    root.configure(background="#243b53")
    tk.Label(root, text="Enter vendor quote", background="#243b53", foreground="white").place(x=30, y=20)
    entry = tk.Entry(root, width=35)
    entry.place(x=30, y=65)

    def save(event=None):
        target.write_text(entry.get(), encoding="utf-8")
        root.title("Saved: " + entry.get())
        root.configure(background="#198754")

    tk.Button(root, text="Save quote", command=save).place(x=30, y=115)
    root.bind("<Return>", save)
    entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()
