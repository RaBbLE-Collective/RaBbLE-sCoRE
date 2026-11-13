# Python-UI baseline probe (S292). Run from RaBbLE-sCoRE/ with the rabble-ui venv python. Re-run after changes; the Rust bench must beat these.
import asyncio, time
from textual.app import App
from ui.face import FaceWidget
class A(App):
    def compose(self):
        yield FaceWidget(detail="full", classes="-full")
async def main():
    app=A()
    async with app.run_test(size=(200,55)) as p:
        w=app.query_one(FaceWidget)
        await p.pause(1.5)
        orig=w._tick; ts=[]
        def timed():
            t=time.perf_counter(); orig(); ts.append((time.perf_counter()-t)*1000)
        w._timer.stop(); w._timer = w.set_interval(1/30, timed)
        await p.pause(4)
        ts.sort(); print("tick ms n=%d p50 %.1f p95 %.1f max %.1f"%(len(ts),ts[len(ts)//2],ts[int(len(ts)*.95)],ts[-1]))
asyncio.run(main())
