// =============================================================================
//  doomgeneric backend для Protogen LED-матрицы (64×32)
// =============================================================================
//
//  Реализует функции doomgeneric (DG_*) с двумя задачами:
//    1. Принимает DOOM-кадры 320×200 ARGB, кропает до 320×160 (аспект 2:1)
//       и даунсэмплит 5× → 64×32 px → выводит на HUB75 матрицу через
//       rpi-rgb-led-matrix.
//    2. Читает джойстик-события с /dev/input/js0 (DualShock 4 paired через
//       BlueZ), маппит кнопки/стики на DOOM-клавиши.
//
//  Запускать ОТ ROOT (нужен доступ к /dev/mem для GPIO matrix).
//  Перед запуском убедись что протоген ОСТАНОВЛЕН — иначе конфликт за матрицу.
//
//  Сборка: см. Makefile.matrix в этой же папке.
// =============================================================================

extern "C" {
    #include "doomkeys.h"
    #include "doomgeneric.h"
    #include "m_argv.h"
}

#include <led-matrix.h>

#include <chrono>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>
#include <linux/joystick.h>

using namespace rgb_matrix;

// ── matrix ──────────────────────────────────────────────────────────────────
static RGBMatrix*    g_matrix = nullptr;
static FrameCanvas*  g_canvas = nullptr;

// ── joystick (DualShock 4) ──────────────────────────────────────────────────
static int g_js_fd = -1;

// ── event queue для DG_GetKey ──────────────────────────────────────────────
#define EVQ_SIZE 64
struct DoomEvent { int pressed; int key; };
static DoomEvent g_evq[EVQ_SIZE];
static int g_evq_head = 0;
static int g_evq_tail = 0;

static inline void evq_push(int pressed, int key) {
    int next = (g_evq_tail + 1) % EVQ_SIZE;
    if (next == g_evq_head) return;  // overflow — drop
    g_evq[g_evq_tail].pressed = pressed;
    g_evq[g_evq_tail].key     = key;
    g_evq_tail = next;
}

static inline int evq_pop(int* pressed, unsigned char* key) {
    if (g_evq_head == g_evq_tail) return 0;
    *pressed = g_evq[g_evq_head].pressed;
    *key     = (unsigned char) g_evq[g_evq_head].key;
    g_evq_head = (g_evq_head + 1) % EVQ_SIZE;
    return 1;
}

// ── joystick button → DOOM key map ─────────────────────────────────────────
//
// DualShock 4 layout (под /dev/input/js0):
//   buttons:  0=Square 1=Cross 2=Circle 3=Triangle
//             4=L1     5=R1    6=L2     7=R2
//             8=Share  9=Options 10=PS  11=L3  12=R3
//   axes:     0=LX 1=LY 2=L2-trig 3=RX 4=RY 5=R2-trig 6=Dpad-X 7=Dpad-Y
//
struct ButtonMap { int doom_key; bool pressed; };
static ButtonMap g_buttons[16] = {
    // Face-кнопки: USE (открыть дверь) на 0/2, ENTER (меню) на 1/3.
    // В игре чаще нужна дверь, поэтому 2 кнопки на USE.
    /* 0 face */ { KEY_USE,   false },
    /* 1 face */ { KEY_ENTER, false },
    /* 2 face */ { KEY_USE,   false },
    /* 3 face */ { KEY_ENTER, false },

    /* 4 L1 */ { ',',          false },   // strafe LEFT
    /* 5 R1 */ { '.',          false },   // strafe RIGHT
    /* 6 L2 */ { KEY_RSHIFT,   false },   // RUN
    /* 7 R2 */ { KEY_FIRE,     false },   // FIRE

    /* 8 Share   */ { KEY_ESCAPE, false },  // ESC (выход из меню)
    /* 9 Options */ { KEY_TAB,    false },  // MAP

    /*10 PS  */ { 0, false },
    /*11 L3  */ { 0, false },
    /*12 R3  */ { KEY_USE, false },   // правый стик-кнопка = USE (открыть дверь)
};

// Per-axis virtual button states (negative dir, positive dir).
// Для L2/R2 (AXIS 2 / 5): отпущен = -32767, нажат полностью = +32767.
// Кладём действие в pos_key — сработает при пересечении порога вверх.
struct AxisMap { int neg_key, pos_key; bool neg_on, pos_on; };
static AxisMap g_axes[8] = {
    /* 0 LX  */ { ',',           '.',            false, false },  // L-стик X = STRAFE
    /* 1 LY  */ { KEY_UPARROW,   KEY_DOWNARROW,  false, false },  // L-стик Y = вперёд/назад
    /* 2 L2T */ { 0, KEY_RSHIFT,                 false, false },  // L2 = RUN
    /* 3 RX  */ { KEY_LEFTARROW, KEY_RIGHTARROW, false, false },  // R-стик X = ПОВОРОТ
    /* 4 RY  */ { 0, 0,                          false, false },  // R-стик Y = не используется
    /* 5 R2T */ { 0, KEY_FIRE,                   false, false },  // R2 = FIRE
    /* 6 DpX */ { ',', '.',                       false, false },
    /* 7 DpY */ { KEY_UPARROW,   KEY_DOWNARROW,  false, false },
};

static const int16_t AXIS_DEADZONE = 8000;

static void poll_joystick() {
    if (g_js_fd < 0) return;
    struct js_event je;
    while (read(g_js_fd, &je, sizeof(je)) == sizeof(je)) {
        je.type &= ~JS_EVENT_INIT;
        if (je.type == JS_EVENT_BUTTON && je.number < 16) {
            fprintf(stderr, "BUTTON %d value=%d\n", je.number, je.value);

            // Спец-кнопка: PS (BUTTON 10) — мгновенный выход из DOOM.
            // run_doom.sh после exit вернёт протогена.
            if (je.number == 10 && je.value != 0) {
                fprintf(stderr, "[doom] PS button — exit\n");
                delete g_matrix;
                exit(0);
            }

            ButtonMap& b = g_buttons[je.number];
            if (b.doom_key == 0) continue;
            bool now = (je.value != 0);
            if (now != b.pressed) {
                b.pressed = now;
                evq_push(now ? 1 : 0, b.doom_key);
            }
        }
        else if (je.type == JS_EVENT_AXIS && je.number < 8) {
            // Печатать только значимые отклонения, чтобы не засорять лог
            // микро-дрожанием стика в нуле.
            if (je.value > 5000 || je.value < -5000) {
                fprintf(stderr, "AXIS %d value=%d\n", je.number, je.value);
            }
            AxisMap& a = g_axes[je.number];
            if (a.neg_key == 0 && a.pos_key == 0) continue;
            bool neg_on = (je.value < -AXIS_DEADZONE);
            bool pos_on = (je.value >  AXIS_DEADZONE);
            if (neg_on != a.neg_on) {
                a.neg_on = neg_on;
                if (a.neg_key) evq_push(neg_on ? 1 : 0, a.neg_key);
            }
            if (pos_on != a.pos_on) {
                a.pos_on = pos_on;
                if (a.pos_key) evq_push(pos_on ? 1 : 0, a.pos_key);
            }
        }
    }
}

// =============================================================================
//                           doomgeneric API
// =============================================================================

void DG_Init() {
    // ── matrix init ──────────────────────────────────────────────────────
    RGBMatrix::Options opts;
    opts.rows             = 32;
    opts.cols             = 64;
    opts.chain_length     = 1;
    opts.parallel         = 1;
    opts.hardware_mapping = "regular";
    opts.brightness       = 80;
    opts.pwm_bits         = 8;          // меньше PWM bits → больше FPS
    opts.disable_hardware_pulsing = true;

    RuntimeOptions rt;
    rt.gpio_slowdown   = 2;
    rt.drop_privileges = -1;            // оставляем root для матрицы

    g_matrix = RGBMatrix::CreateFromOptions(opts, rt);
    if (!g_matrix) {
        fprintf(stderr, "[doom] Не удалось создать матрицу.\n");
        exit(1);
    }
    g_canvas = g_matrix->CreateFrameCanvas();

    // ── joystick init ────────────────────────────────────────────────────
    g_js_fd = open("/dev/input/js0", O_RDONLY | O_NONBLOCK);
    if (g_js_fd < 0) {
        fprintf(stderr, "[doom] Внимание: /dev/input/js0 не найден — нет джойстика.\n");
    } else {
        fprintf(stderr, "[doom] Джойстик подключен на /dev/input/js0.\n");
    }
}

void DG_DrawFrame() {
    // DG_ScreenBuffer — uint32_t[DOOMGENERIC_RESY * DOOMGENERIC_RESX], ARGB8888.
    // Кропаем сверху/снизу до 320×160 (центральная полоса), чтобы аспект
    // соответствовал 64×32 (2:1) — без вертикального сжатия.

    const int SRC_W   = DOOMGENERIC_RESX;          // 320
    const int SRC_H   = DOOMGENERIC_RESY;          // 200
    const int CROP_T  = (SRC_H - 160) / 2;          // 20
    const int STEP    = 5;                          // 320/64 = 5, 160/32 = 5

    for (int y = 0; y < 32; y++) {
        for (int x = 0; x < 64; x++) {
            int sx = x * STEP + STEP / 2;
            int sy = CROP_T + y * STEP + STEP / 2;
            uint32_t px = DG_ScreenBuffer[sy * SRC_W + sx];
            uint8_t r = (px >> 16) & 0xFF;
            uint8_t g = (px >> 8)  & 0xFF;
            uint8_t b =  px        & 0xFF;
            g_canvas->SetPixel(x, y, r, g, b);
        }
    }
    g_canvas = g_matrix->SwapOnVSync(g_canvas);
}

void DG_SleepMs(uint32_t ms) {
    usleep(ms * 1000);
}

uint32_t DG_GetTicksMs() {
    auto now   = std::chrono::steady_clock::now();
    auto since = now.time_since_epoch();
    return std::chrono::duration_cast<std::chrono::milliseconds>(since).count();
}

int DG_GetKey(int* pressed, unsigned char* doomKey) {
    poll_joystick();
    return evq_pop(pressed, doomKey);
}

void DG_SetWindowTitle(const char* title) {
    (void) title;   // нет окна — игнор
}

// =============================================================================
//                                   main
// =============================================================================

int main(int argc, char** argv) {
    fprintf(stderr, "[doom] Запуск doomgeneric на LED-матрице 64×32\n");
    doomgeneric_Create(argc, argv);
    while (true) {
        doomgeneric_Tick();
    }
    return 0;
}
