# Reading match video instead of the replay file

Notes from probing whether computer vision on the broadcast video can supply
what the binary replay does not — chiefly which unit dealt each hit.

## The video matches the replay

`youtube.com/watch?v=9LJmiWhRR20` — 【236联武神坛】曲阜孔庙 VS 紫禁城（决赛），
101.7 minutes, uploaded 2026-03-15. It is the same battle as
`紫禁城VS曲阜孔庙.mhw`, whose sync frames stamp 2026-03-16 09:48 Beijing.

The direct NetEase URL form is
`https://xyq-video.v.netease.com/video/pk/{id}_{date}_{time}_{quality}.mp4`
and needs no login, but the file it serves is a different match.

## What the frame gives away

At 720p a single frame carries more labelled state than expected:

- Team banners name both sides. The δ-suffixed players are 曲阜孔庙 and the
  ．-suffixed players are 紫禁城, which is what ties the replay's unit ids
  1..10 and 11..20 to real team names.
- A round counter, 第 N 回合, sits top-centre in an orange game font.
- Every unit on the field, pets included, carries its name in green text.
- Hit point bars float above each unit, and damage numbers appear on hit.

## Round OCR works

Crop `[10:45, 570:690]`, upscale 8x, keep pixels with `r>150, g<160, b<130`
to isolate the orange digits, tight-crop to their bounding box, pad, then
run tesseract with `--psm 8` and a digits whitelist. Modes 7 and 10 return
nothing on this font; 8 and 13 both read it.

Over a 300-second probe the counter stepped 8 → 9 at t=89s → 10 at t=164s,
so roughly 75-80 seconds per round. At that rate the replay's 63 rounds run
about 81 minutes, which fits inside the 101.7-minute video with room for
the pre-match and post-match segments. Reads succeeded on 250 of 300
sampled seconds; the failures are misreads (6, 40, 1, 0) that majority
voting over neighbouring frames should absorb.

## Finding the attacker by vision does not work naively

Two approaches were tried and both failed for the same underlying reason —
the arena is saturated with looping animation.

**Frame differencing.** Motion never localises to an acting unit. Across a
5-second window the high-motion frames all put their centroid within
(330-390, 170-200) and recur every ~6 frames, i.e. a 0.2s animation loop.
Idle animations, shield bubbles and spell auras all move continuously, so
"what moved" does not separate the attacker from the scenery.

**Colour-keyed damage text.** Masking for bright red picks up persistent
flame and aura effects rather than floating numbers. The blobs it finds sit
at fixed coordinates — (725,292), (586,393), (733,183) — in all 120 sampled
seconds, which is the signature of decoration, not of text that appears and
fades.

Making either work needs per-slot background modelling: learn each unit's
idle appearance at its formation position, then flag deviation from it.
Formation slots are static, so this is tractable, but it is a real piece of
work rather than a quick filter.

## What to try next

The round counter already aligns video time to replay rounds, so labelling
can be scoped to one round at a time rather than a 101-minute scrub. The
cheapest useful next step is to sample a single round at high frame rate,
model each formation slot's idle frame, and check whether "unit left its
slot" separates cleanly. If it does, a few hundred labelled hits are enough
to test any candidate decoding of the attacker field.
