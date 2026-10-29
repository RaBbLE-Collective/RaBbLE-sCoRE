# Python-UI baseline probe (S292). Run from RaBbLE-sCoRE/ with the rabble-ui venv python. Re-run after changes; the Rust bench must beat these.
import asyncio, cProfile, pstats
from textual.app import App
from ui.face import FaceWidget
class A(App):
    def compose(self):
        yield FaceWidget(detail="full", classes="-full")
async def main():
    app=A()
    async with app.run_test(size=(200,55)) as p:
        await p.pause(1.5)
        pr=cProfile.Profile(); pr.enable(); await p.pause(4); pr.disable()
        pstats.Stats(pr).sort_stats("tottime").print_stats(14)
asyncio.run(main())
