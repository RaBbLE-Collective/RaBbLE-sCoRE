# Python-UI baseline probe (S292). Run from RaBbLE-sCoRE/ with the rabble-ui venv python. Re-run after changes; the Rust bench must beat these.
import asyncio, time, sys
from textual.app import App
from textual import events
from ui.widgets.transcript import Transcript
LONG="\n\n".join(f"paragraph {i} with some `code` text and **bold** words to wrap around a fairly wide line "*2 for i in range(200))
class A(App):
    def compose(self):
        self.t=Transcript(lambda o:"white"); yield self.t
async def main(fps):
    app=A(); n=[0]; cost=[]
    orig=app._display
    def disp(screen, r):
        t=time.perf_counter(); orig(screen,r); cost.append((time.perf_counter()-t)*1000); n[0]+=1
    app._display=disp
    async with app.run_test(size=(196,50)) as p:
        await app.t.delta("lead",LONG); await p.pause(1)
        t=app.t; t.GLIDE_FPS=fps
        n[0]=0; cost.clear(); t0=time.perf_counter()
        for _ in range(8):
            t.post_message(events.MouseScrollUp(t,1,1,0,0,0,False,False,False)); await p.pause(0.04)
        await p.pause(0.6)
        dt=time.perf_counter()-t0
        cost.sort(); print(f"glide fps={fps}: displays {n[0]} in {dt:.2f}s = {n[0]/dt:.0f}/s, display cost p50 {cost[len(cost)//2]:.1f}ms max {cost[-1]:.1f}ms")
asyncio.run(main(int(sys.argv[1])))
