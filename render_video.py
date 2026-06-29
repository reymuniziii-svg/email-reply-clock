"""Render the reply-clock posting asset (MP4 + GIF + poster) from the public-safe data.
Discrete shaded beads (depth, low glow) composited over a dark field. Deterministic.
Usage: python3 render_video.py [out_dir]
"""
import numpy as np, json, math, os, sys, subprocess, tempfile, shutil
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "reply_clock.json")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "export")
os.makedirs(OUT, exist_ok=True)

DIDOT = "/System/Library/Fonts/Supplemental/Didot.ttc"
AVENIR = "/System/Library/Fonts/Avenir Next.ttc"
AV = {"regular": 7, "medium": 5, "demi": 2, "bold": 0}
BG = np.array([13, 11, 20]) / 255.0
INK, MUTED, FAINT, LABEL = (244,238,230),(174,172,190),(120,118,142),(150,147,172)

def didot(sz, italic=False):
    try: return ImageFont.truetype(DIDOT, sz, index=1 if italic else 0)
    except Exception: return ImageFont.truetype("/System/Library/Fonts/Supplemental/Georgia.ttf", sz)
def av(sz, w="regular"):
    try: return ImageFont.truetype(AVENIR, sz, index=AV[w])
    except Exception: return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", sz)

d = json.load(open(DATA))
meta = d["meta"]; rows = d["rows"]
CAP = meta["cap_minutes"]; LOGCAP = math.log(CAP + 1)
ROT = (meta["mean_hour"] / 24) * 2 * math.pi
maxday = meta["archive_day_count"]; RINGS = meta["rings"]

STOPS = [(0.0,(255,210,118)),(0.20,(245,156,66)),(0.42,(230,98,62)),(0.66,(172,66,142)),(0.84,(82,108,164)),(1.0,(70,82,150))]
def ramp(t):
    t = min(max(t,0),1)
    for i in range(len(STOPS)-1):
        a,ca=STOPS[i]; b,cb=STOPS[i+1]
        if t<=b:
            f=(t-a)/(b-a) if b>a else 0
            return tuple(ca[k]+(cb[k]-ca[k])*f for k in range(3))
    return STOPS[-1][1]
def tof(lat): return min(1.0, math.log(lat+1)/LOGCAP)

S = 1200
cx = S/2; cy = S*0.55; rOut = S*0.31; rIn = max(4, rOut*0.028)
def radius(t): return rIn + t*(rOut-rIn)
def ease(p): return 1-(1-min(max(p,0),1))**3

# ---- bead sprite with depth (shaded sphere + faint halo) ----
BUCKETS = 48
RC = 3.6          # core radius (px)
RH = 7            # halo radius (px)
L = np.array([-0.42, -0.5, 0.76]); L = L/np.linalg.norm(L)
def make_bead(color):
    n = RH*2+1; c = RH
    ys, xs = np.mgrid[0:n, 0:n]
    dx = xs-c; dy = ys-c; dist = np.sqrt(dx*dx+dy*dy)
    base = np.array(color)/255.0
    rgb = np.ones((n,n,3))*BG
    a = np.zeros((n,n))
    inside = dist < RC
    z = np.sqrt(np.clip(RC*RC - dx*dx - dy*dy, 0, None))
    nx, ny, nz = dx/RC, dy/RC, z/RC
    ndl = np.clip(nx*L[0]+ny*L[1]+nz*L[2], 0, 1)
    shade = 0.42 + 0.72*ndl
    spec = (ndl**16)*0.55
    core_rgb = np.clip(base[None,None,:]*shade[...,None] + spec[...,None], 0, 1)
    core_a = np.clip((RC + 0.6 - dist)/1.4, 0, 1)          # soft 1px edge
    halo_a = 0.13*np.exp(-(dist*dist)/(2*(RH*0.55)**2))     # cooled-down glow
    halo_a = np.where(inside, 0, halo_a)
    a = np.maximum(core_a, halo_a)
    rgb = np.where((core_a >= halo_a)[...,None], core_rgb, base[None,None,:])
    return rgb.astype(np.float32), a.astype(np.float32)

BEADS = [make_bead(ramp(b/(BUCKETS-1))) for b in range(BUCKETS)]

def comp(buf, x, y, bucket, ga):
    rgb, a0 = BEADS[bucket]
    n = rgb.shape[0]; r = n//2
    xi, yi = int(round(x)), int(round(y))
    x0,x1,y0,y1 = xi-r, xi+r+1, yi-r, yi+r+1
    H,W,_ = buf.shape
    if x1<=0 or y1<=0 or x0>=W or y0>=H: return
    sx0,sy0 = max(0,-x0), max(0,-y0)
    sx1,sy1 = n-max(0,x1-W), n-max(0,y1-H)
    x0,y0,x1,y1 = max(0,x0),max(0,y0),min(W,x1),min(H,y1)
    a = a0[sy0:sy1, sx0:sx1]*ga
    src = rgb[sy0:sy1, sx0:sx1]
    reg = buf[y0:y1, x0:x1]
    reg[:] = reg*(1-a[...,None]) + src*a[...,None]

# precompute per point; z-order slow->fast so the warm core sits on top
rng = np.random.default_rng(7)
P = []
for hour, lat, day in rows:
    t = tof(lat)
    P.append(dict(ang=-math.pi/2+((hour/24)*2*math.pi-ROT), t=t,
                  spawn=(day/maxday)*0.84, travel=0.012+t*0.075,
                  bucket=min(BUCKETS-1, round(t*(BUCKETS-1))),
                  jr=rng.normal(0,1), ja=rng.normal(0,1),
                  al=0.95 if t<0.5 else 0.82))
P.sort(key=lambda p: -p["t"])

def tracked(dr, x, y, s, font, fill, tr=0, align="left"):
    ws=[dr.textlength(ch,font=font) for ch in s]; total=sum(ws)+tr*(len(s)-1)
    cx0 = x-total if align=="right" else (x-total/2 if align=="center" else x)
    for ch,w in zip(s,ws): dr.text((cx0,y),ch,font=font,fill=fill); cx0+=w+tr
def halo(dr,xy,s,fnt,fill,anchor="mm"):
    dr.text(xy,s,font=fnt,fill=fill,anchor=anchor,stroke_width=4,stroke_fill=(8,6,14))

def chrome(img, fade=1.0):
    dr = ImageDraw.Draw(img,"RGBA"); a=int(255*fade); fr=av(25)
    for lat,lab in RINGS:
        r=radius(tof(lat)); dr.ellipse([cx-r,cy-r,cx+r,cy+r],outline=(150,146,175,int(50*fade)),width=1)
        if lat==1: continue
        halo(dr,(cx,cy+r),lab,fr,(188,186,204,a))
    for lab,hour in [("midnight",0),("6am",6),("noon",12),("6pm",18)]:
        ang=-math.pi/2+((hour/24)*2*math.pi-ROT); r0,r1=rOut+12,rOut+22
        dr.line([cx+r0*math.cos(ang),cy+r0*math.sin(ang),cx+r1*math.cos(ang),cy+r1*math.sin(ang)],fill=(150,150,178,a),width=2)
        halo(dr,(cx+(rOut+48)*math.cos(ang),cy+(rOut+48)*math.sin(ang)),lab,fr,(198,196,214,a))

def overlay(img):
    dr=ImageDraw.Draw(img,"RGBA"); M=76
    dr.text((M,70),"How fast I reply",font=didot(72),fill=INK)
    sub=av(25)
    dr.text((M,168),"8,341 work email replies over three years.",font=sub,fill=MUTED)
    dr.text((M,202),"Distance from the center is the time it took me to reply;",font=sub,fill=MUTED)
    dr.text((M,236),"angle is the hour of day.",font=sub,fill=MUTED)
    lw,lh,lx,ly=260,9,M,300
    for i in range(lw): dr.line([lx+i,ly,lx+i,ly+lh],fill=tuple(int(c) for c in ramp(i/(lw-1))))
    tracked(dr,lx,ly+lh+12,"FASTER",av(16,"medium"),LABEL,tr=3)
    tracked(dr,lx+lw,ly+lh+12,"SLOWER",av(16,"medium"),LABEL,tr=3,align="right")
    for (lab,val),y in zip([("REPLIES","8,341"),("WITHIN THE HOUR","61.9%"),("MEDIAN REPLY","28 min")],[78,182,286]):
        tracked(dr,S-M,y,lab,av(16,"medium"),LABEL,tr=3,align="right")
        dr.text((S-M,y+26),val,font=didot(46),fill=INK,anchor="ra")
    tracked(dr,M,S-46,"SOURCE: MY WORK EMAIL ARCHIVE   ·   TOOL: PYTHON",av(15,"medium"),FAINT,tr=2)

def render_frame(p):
    if p<=0.93: bp=p; fade=1.0
    else: bp=0.93; fade=max(0.0,1-(p-0.93)/0.07)
    buf = np.ones((S,S,3),np.float32)*BG
    for pt in P:
        if bp < pt["spawn"]: continue
        prog = ease((bp-pt["spawn"])/pt["travel"])
        rf = radius(pt["t"]) + pt["jr"]*(rOut*0.011)
        r = rIn + (rf-rIn)*prog
        aa = pt["ang"] + pt["ja"]*0.018
        comp(buf, cx+r*math.cos(aa), cy+r*math.sin(aa), pt["bucket"], pt["al"]*(0.6+0.4*prog)*fade)
    arr = np.clip(buf*255,0,255).astype(np.uint8)
    img = Image.fromarray(arr)
    chrome(img,fade); overlay(img)
    return img

def main():
    FPS=30; SEC=18; F=FPS*SEC
    tmp=tempfile.mkdtemp(); print(f"rendering {F} frames at {S}x{S}...")
    for i in range(F):
        render_frame(i/(F-1)).save(os.path.join(tmp,f"f{i:04d}.png"))
        if i%60==0: print("  frame",i)
    render_frame(0.9).save(os.path.join(OUT,"reply_clock_poster.png"))
    mp4=os.path.join(OUT,"reply_clock.mp4")
    subprocess.run(["ffmpeg","-y","-framerate",str(FPS),"-i",os.path.join(tmp,"f%04d.png"),
                    "-c:v","libx264","-pix_fmt","yuv420p","-crf","17","-movflags","+faststart",mp4],check=True,capture_output=True)
    pal=os.path.join(tmp,"pal.png")
    subprocess.run(["ffmpeg","-y","-i",mp4,"-vf","fps=15,scale=560:-1:flags=lanczos,palettegen=max_colors=160",pal],check=True,capture_output=True)
    subprocess.run(["ffmpeg","-y","-i",mp4,"-i",pal,"-lavfi","fps=15,scale=560:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4",os.path.join(OUT,"reply_clock.gif")],check=True,capture_output=True)
    shutil.rmtree(tmp)
    for f in ["reply_clock.mp4","reply_clock.gif","reply_clock_poster.png"]:
        print(f, f"{os.path.getsize(os.path.join(OUT,f))/1e6:.2f} MB")
    print("DONE ->",OUT)

if __name__=="__main__": main()
