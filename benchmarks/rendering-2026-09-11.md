# Rendering benchmark

UTC: 2026-09-11T18:39:53.866931+00:00

Environment: `{'platform': 'Windows-11-10.0.22631-SP0', 'processor': '11th Gen Intel(R) Core(TM) i7-11800H @ 2.30GHz', 'model': None, 'logical_cpus': 16, 'python': '3.13.5', 'numpy': '2.5.2', 'pillow': '12.3.0'}`

Real render loop with synthetic input and dummy matrix; no GPIO/VSync or sleep. This measures CPU-side frame construction, not hardware display FPS.

Parameters: `{'frames_per_repeat': 600, 'warmup_frames': 120, 'repeats': 7, 'micro_iterations_per_repeat': 1000, 'seed': 314159, 'virtual_fps': 30}`

## Actual render loop (CPU work only)

| Scenario | Before median, ms | After median, ms | After p95, ms | Speedup |
|---|---:|---:|---:|---:|
| solid | 0.845 | 0.699 | 1.127 | 1.21x |
| gradient | 1.092 | 0.701 | 1.030 | 1.56x |
| rainbow | 1.042 | 0.810 | 1.413 | 1.29x |
| party_rainbow | 1.069 | 0.829 | 1.415 | 1.29x |
| text_rainbow_x3 | 1.243 | 0.375 | 0.732 | 3.32x |

## Individual operations

Values are medians of batch averages; unchanged gradients use a warm cache.

| Operation | Before, us | After, us | Speedup |
|---|---:|---:|---:|
| animated_face | 214.05 | 65.25 | 3.28x |
| static_face | 193.75 | 31.96 | 6.06x |
| gradient_unchanged | 205.64 | 0.66 | 312.95x |
| rainbow_fill | 175.62 | 79.72 | 2.20x |
| rainbow_text_x1 | 387.99 | 231.69 | 1.67x |
| rainbow_text_x3 | 1508.29 | 372.68 | 4.05x |

## Validation

| Scenario | Frames per version | Baseline pixel equality |
|---|---:|---|
| solid | 180 | Pass |
| gradient | 180 | Pass |
| rainbow | 180 | Pass |
| party_rainbow | 180 | Pass |
| text_rainbow_x3 | 180 | Pass |

Loop p95 is calculated across individual frames. Old/new run order alternates between repeats. Random seeds, audio and virtual clock match between versions. The 128x32 output uses two mirrored 64x32 panels. GPIO copy, VSync, frame pacing, boot and concurrent web/serial activity are excluded. Physical refresh rate and overall Raspberry Pi CPU usage cannot be inferred from these values.
