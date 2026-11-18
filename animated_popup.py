import tkinter as tk


def animated_popup(title="Notice", message="Hello!", duration=3000):
    root = tk.Tk()
    root.withdraw()

    popup = tk.Toplevel(root)
    popup.title(title)
    popup.overrideredirect(True)
    popup.attributes("-alpha", 0.0)
    popup.attributes("-topmost", True)

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    width, height = 320, 120
    x = (screen_w - width) // 2
    y = screen_h - height - 60
    popup.geometry(f"{width}x{height}+{x}+{y}")

    frame = tk.Frame(popup, bg="#2b2b2b", bd=2, relief="flat")
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame, text=title, bg="#2b2b2b", fg="#ffffff",
        font=("Helvetica", 12, "bold"), pady=8
    ).pack()

    tk.Label(
        frame, text=message, bg="#2b2b2b", fg="#cccccc",
        font=("Helvetica", 10), wraplength=280
    ).pack(padx=12)

    def fade_in(alpha=0.0):
        alpha = min(alpha + 0.05, 1.0)
        popup.attributes("-alpha", alpha)
        if alpha < 1.0:
            popup.after(20, fade_in, alpha)
        else:
            popup.after(duration, fade_out)

    def fade_out(alpha=1.0):
        alpha = max(alpha - 0.05, 0.0)
        popup.attributes("-alpha", alpha)
        if alpha > 0.0:
            popup.after(20, fade_out, alpha)
        else:
            popup.destroy()
            root.destroy()

    popup.after(50, fade_in)
    root.mainloop()


if __name__ == "__main__":
    animated_popup(
        title="Hello!",
        message="This is an animated popup. It fades in and out automatically.",
        duration=3000,
    )
