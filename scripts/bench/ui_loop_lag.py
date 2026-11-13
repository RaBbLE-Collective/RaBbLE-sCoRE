# Python-UI baseline probe (S292). Run from RaBbLE-sCoRE/ with the rabble-ui venv python. Re-run after changes; the Rust bench must beat these.
import asyncio, time, statistics
from textual.app import App
from ui.face import FaceWidget
class A(App):
    def compose(self):
        yield FaceWidget(detail="full", classes="-full")
async def main():
    app=A()
    async with app.run_test(size=(200,55)) as p:
        await p.pause(1.5)
        lag=[]
        for _ in range(300):
            t=time.perf_counter(); await asyncio.sleep(0.01); lag.append((time.perf_counter()-t-0.01)*1000)
        lag.sort()
        print("loop lag ms  p50 %.1f p95 %.1f p99 %.1f max %.1f"%(lag[150],lag[285],lag[297],lag[-1]))
asyncio.run(main())
