// =============================================================================
//  Bad Apple!! плеер на LED-матрице протогена (64×32)
// =============================================================================
//
//  Читает raw grayscale-поток кадров 64×32 (см. prepare_frames.sh) и
//  выводит на HUB75 матрицу через rpi-rgb-led-matrix.
//
//  Аудио проигрывается параллельно (fork + exec mpg123 если MP3 найден рядом).
//
//  Запуск от root, при остановленном протогене:
//    sudo ./bad_apple_player bad_apple.gray [bad_apple.mp3]
// =============================================================================

#include <led-matrix.h>

#include <chrono>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <fcntl.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/types.h>
#include <signal.h>
#include <linux/joystick.h>

using namespace rgb_matrix;

static const int W = 64;
static const int H = 32;
static const int FPS = 30;

// Bad Apple = много белого экрана → матрица кушает ~2× больше тока чем DOOM.
// На слабом БП Pi перезагружается. Держим яркость низкой + режем пик через
// более тёмный порог.
static const int MAX_WHITE = 90;   // максимальная яркость пикселя (0..255)

// ── joystick — только выход по PS-кнопке ────────────────────────────────
static const int EXIT_BUTTON = 10;   // PS

static bool poll_exit(int fd) {
    if (fd < 0) return false;
    struct js_event je;
    while (read(fd, &je, sizeof(je)) == sizeof(je)) {
        je.type &= ~JS_EVENT_INIT;
        if (je.type == JS_EVENT_BUTTON
                && je.number == EXIT_BUTTON
                && je.value != 0) {
            return true;
        }
    }
    return false;
}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s frames.gray\n", argv[0]);
        return 1;
    }

    FILE* f = fopen(argv[1], "rb");
    if (!f) {
        fprintf(stderr, "Cannot open %s\n", argv[1]);
        return 1;
    }

    // ── init matrix ──────────────────────────────────────────────────────
    RGBMatrix::Options opts;
    opts.rows             = 32;
    opts.cols             = 64;
    opts.chain_length     = 1;
    opts.parallel         = 1;
    opts.hardware_mapping = "regular";
    opts.brightness       = 50;    // низкая яркость чтобы Pi не ушла в reboot
    opts.pwm_bits         = 6;
    opts.disable_hardware_pulsing = true;

    RuntimeOptions rt;
    rt.gpio_slowdown   = 2;
    rt.drop_privileges = -1;

    RGBMatrix* matrix = RGBMatrix::CreateFromOptions(opts, rt);
    if (!matrix) {
        fprintf(stderr, "Cannot create matrix\n");
        return 1;
    }
    FrameCanvas* canvas = matrix->CreateFrameCanvas();

    // ── joystick ─────────────────────────────────────────────────────────
    int js_fd = open("/dev/input/js0", O_RDONLY | O_NONBLOCK);
    if (js_fd >= 0) {
        fprintf(stderr, "[bad_apple] joystick — press PS to exit\n");
    }

    // ── playback loop ────────────────────────────────────────────────────
    uint8_t frame[W * H];
    auto start = std::chrono::steady_clock::now();
    int frame_idx = 0;

    while (fread(frame, 1, W * H, f) == (size_t)(W * H)) {
        if (poll_exit(js_fd)) {
            fprintf(stderr, "[bad_apple] PS pressed — exit\n");
            break;
        }

        for (int y = 0; y < H; y++) {
            for (int x = 0; x < W; x++) {
                uint8_t v = frame[y * W + x];
                // Ограничиваем пиковый белый чтобы не сжечь БП.
                if (v > MAX_WHITE) v = MAX_WHITE;
                canvas->SetPixel(x, y, v, v, v);
            }
        }
        canvas = matrix->SwapOnVSync(canvas);

        frame_idx++;
        auto target = start + std::chrono::microseconds(
            (long long)frame_idx * 1000000LL / FPS);
        std::this_thread::sleep_until(target);
    }

    fclose(f);
    if (js_fd >= 0) close(js_fd);
    delete matrix;

    fprintf(stderr, "[bad_apple] played %d frames\n", frame_idx);
    return 0;
}
