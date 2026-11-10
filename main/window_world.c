#include "window_world.h"

#include <math.h>
#include <string.h>
#include <time.h>

#include "esp_random.h"

LV_FONT_DECLARE(ui_font_fixed_20);
LV_FONT_DECLARE(ui_font_detail_20);

#define SCREEN 466
#define HORIZON 302
#define GROUND_TOP 330
#define STAR_COUNT 10
#define RAIN_COUNT 14
#define SNOW_COUNT 12
#define CLOUD_COUNT 3
#define SMOKE_COUNT 3

#define COLOR_TEXT 0xF1F8F5
#define COLOR_MUTED 0xC7D2CE

static lv_obj_t *s_screen;
static lv_obj_t *s_sky;
static lv_obj_t *s_sun;
static lv_obj_t *s_moon;
static lv_obj_t *s_stars[STAR_COUNT];
static lv_obj_t *s_clouds[CLOUD_COUNT];
static lv_obj_t *s_hill_far;
static lv_obj_t *s_hill_near;
static lv_obj_t *s_ground;
static lv_obj_t *s_factory;
static lv_obj_t *s_factory_roof;
static lv_obj_t *s_chimney;
static lv_obj_t *s_factory_window;
static lv_obj_t *s_smoke[SMOKE_COUNT];
static lv_obj_t *s_runner;
static lv_obj_t *s_runner_leg_left;
static lv_obj_t *s_runner_leg_right;
static lv_obj_t *s_rain[RAIN_COUNT];
static lv_obj_t *s_snow[SNOW_COUNT];
static lv_obj_t *s_fog[2];
static lv_obj_t *s_flash;
static lv_obj_t *s_title;
static lv_obj_t *s_info;
static lv_obj_t *s_clock;
static lv_font_t s_ui_font;

static window_weather_t s_condition;
static bool s_weather_valid;
static bool s_temperature_available;
static int s_temperature;
static char s_weather_text[32];
static char s_city[32];
static bool s_codex_working;
static bool s_bambu_printing;
static uint32_t s_tick;
static int s_lightning_countdown = 200;
static int s_flash_frames;

static lv_obj_t *make_shape(lv_obj_t *parent, int width, int height, uint32_t hex, int radius)
{
    lv_obj_t *shape = lv_obj_create(parent);
    lv_obj_remove_style_all(shape);
    lv_obj_set_size(shape, width, height);
    lv_obj_set_style_bg_color(shape, lv_color_hex(hex), 0);
    lv_obj_set_style_bg_opa(shape, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(shape, radius, 0);
    lv_obj_remove_flag(shape, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    return shape;
}

static void set_hidden(lv_obj_t *obj, bool hidden)
{
    if (hidden) {
        lv_obj_add_flag(obj, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_remove_flag(obj, LV_OBJ_FLAG_HIDDEN);
    }
}

static lv_obj_t *make_label(lv_obj_t *parent, const char *text, uint32_t hex)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, &s_ui_font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(hex), 0);
    return label;
}

static uint32_t lerp_color(uint32_t from, uint32_t to, int permille)
{
    if (permille < 0) permille = 0;
    if (permille > 1000) permille = 1000;
    uint32_t result = 0;
    for (int shift = 0; shift <= 16; shift += 8) {
        uint32_t a = (from >> shift) & 0xFF;
        uint32_t b = (to >> shift) & 0xFF;
        result |= (a + (b - a) * permille / 1000) << shift;
    }
    return result;
}

/* 一天中的光照：0=深夜，1000=正午；日出 6-8 点、日落 17-19 点平滑过渡。 */
static int daylight_permille(float hour)
{
    if (hour < 5.0f || hour >= 20.0f) return 0;
    if (hour < 8.0f) return (int)((hour - 5.0f) / 3.0f * 1000.0f);
    if (hour < 17.0f) return 1000;
    return (int)((20.0f - hour) / 3.0f * 1000.0f);
}

static void update_sky(float hour, int daylight)
{
    /* 日出日落时分（黎明 5-8、黄昏 17-20）天空带暖色。 */
    const bool twilight = (hour >= 5.0f && hour < 8.0f) || (hour >= 17.0f && hour < 20.0f);
    uint32_t top = lerp_color(0x060A18, 0x2E7CD6, daylight);
    uint32_t bottom = lerp_color(0x101C38, 0xA9D6FF, daylight);
    if (twilight) {
        const int warmth = 1000 - daylight > 0 ? (1000 - daylight) / 2 : 0;
        top = lerp_color(top, 0x3B2A55, warmth);
        bottom = lerp_color(bottom, 0xFF9D3D, 1000 - daylight);
    }
    if (s_weather_valid && (s_condition == WINDOW_WEATHER_OVERCAST ||
                            s_condition == WINDOW_WEATHER_RAIN ||
                            s_condition == WINDOW_WEATHER_THUNDER)) {
        /* 阴雨：天空去饱和压暗。 */
        top = lerp_color(top, 0x4A5560, 450);
        bottom = lerp_color(bottom, 0x6E7A84, 450);
    } else if (s_weather_valid && s_condition == WINDOW_WEATHER_FOG) {
        top = lerp_color(top, 0x8A949C, 400);
        bottom = lerp_color(bottom, 0xAAB4BA, 400);
    }
    lv_obj_set_style_bg_color(s_sky, lv_color_hex(top), 0);
    lv_obj_set_style_bg_grad_color(s_sky, lv_color_hex(bottom), 0);

    /* 太阳沿弧线东升西落（6-18 点），月亮走夜晚的另一条弧线。 */
    const bool is_day = hour >= 6.0f && hour < 18.0f;
    float phase = is_day ? (hour - 6.0f) / 12.0f : ((hour >= 18.0f ? hour - 18.0f : hour + 6.0f) / 12.0f);
    if (phase < 0.0f) phase = 0.0f;
    if (phase > 1.0f) phase = 1.0f;
    const float theta = (180.0f - phase * 180.0f) * 3.14159265f / 180.0f;
    const int x = 233 + (int)(200.0f * cosf(theta));
    const int y = HORIZON - (int)(190.0f * sinf(theta));
    lv_obj_set_pos(s_sun, x - 17, y - 17);
    lv_obj_set_pos(s_moon, x - 14, y - 14);
    set_hidden(s_sun, !is_day);
    set_hidden(s_moon, is_day);
    /* 太阳在阴雨云雾雪天被遮蔽。 */
    if (s_weather_valid && s_condition >= WINDOW_WEATHER_OVERCAST) {
        lv_obj_set_style_opa(s_sun, LV_OPA_30, 0);
    } else {
        lv_obj_set_style_opa(s_sun, LV_OPA_COVER, 0);
    }

    /* 星星只在晴朗夜晚出现；雨雪云雾的夜空看不到星星。 */
    const bool clear_sky = !s_weather_valid || s_condition == WINDOW_WEATHER_CLEAR ||
                           s_condition == WINDOW_WEATHER_CLOUDY;
    const bool night = daylight < 350 && clear_sky;
    for (int i = 0; i < STAR_COUNT; ++i) {
        set_hidden(s_stars[i], !night);
    }
    lv_obj_set_style_bg_color(s_ground, lv_color_hex(lerp_color(0x0E1A16, 0x2E5D43, daylight)), 0);
    lv_obj_set_style_bg_color(s_hill_far, lv_color_hex(lerp_color(0x0C1424, 0x4A6B8A, daylight)), 0);
    lv_obj_set_style_bg_color(s_hill_near, lv_color_hex(lerp_color(0x0A1812, 0x3D7050, daylight)), 0);
}

static void update_weather_visibility(void)
{
    const bool valid = s_weather_valid;
    const int clouds = !valid ? 0 :
        s_condition == WINDOW_WEATHER_CLOUDY ? 2 :
        s_condition == WINDOW_WEATHER_CLEAR || s_condition == WINDOW_WEATHER_FOG ? 0 : 3;
    const uint32_t cloud_color =
        s_condition == WINDOW_WEATHER_RAIN || s_condition == WINDOW_WEATHER_THUNDER ? 0x6E7A84 :
        s_condition == WINDOW_WEATHER_OVERCAST ? 0x9AA4AC : 0xF1F4F6;
    for (int i = 0; i < CLOUD_COUNT; ++i) {
        set_hidden(s_clouds[i], i >= clouds);
        for (int part = 0; part < 4; ++part) {
            lv_obj_t *child = lv_obj_get_child(s_clouds[i], part);
            if (child != NULL) lv_obj_set_style_bg_color(child, lv_color_hex(cloud_color), 0);
        }
    }
    const bool rainy = valid && (s_condition == WINDOW_WEATHER_RAIN || s_condition == WINDOW_WEATHER_THUNDER);
    const bool snowy = valid && s_condition == WINDOW_WEATHER_SNOW;
    for (int i = 0; i < RAIN_COUNT; ++i) set_hidden(s_rain[i], !rainy);
    for (int i = 0; i < SNOW_COUNT; ++i) set_hidden(s_snow[i], !snowy);
    for (int i = 0; i < 2; ++i) {
        set_hidden(s_fog[i], !(valid && s_condition == WINDOW_WEATHER_FOG));
    }
    set_hidden(s_runner, !s_codex_working);
    for (int i = 0; i < SMOKE_COUNT; ++i) {
        set_hidden(s_smoke[i], !s_bambu_printing);
    }
    lv_obj_set_style_bg_color(s_factory_window,
                              lv_color_hex(s_bambu_printing ? 0xFFD964 : 0x2A3438), 0);
}

static void update_info(void)
{
    time_t now = time(NULL);
    struct tm local;
    localtime_r(&now, &local);
    if (now > 86400) {
        lv_label_set_text_fmt(s_clock, "%02d:%02d", local.tm_hour, local.tm_min);
    } else {
        lv_label_set_text(s_clock, "--:--");
    }
    if (s_weather_valid && s_weather_text[0] != '\0') {
        if (s_temperature_available) {
            lv_label_set_text_fmt(s_info, "%s · %s · %d°C", s_city, s_weather_text, s_temperature);
        } else {
            lv_label_set_text_fmt(s_info, "%s · %s", s_city, s_weather_text);
        }
    } else {
        lv_label_set_text(s_info, "昼夜流转 · 等待天气数据");
    }
}

static void animate_scene(void)
{
    /* 各组动画只在可见时更新：离线无天气时这些对象全部隐藏，
       每 100ms 不触碰任何隐藏对象，避免无谓的布局与重绘开销。 */
    const bool valid = s_weather_valid;
    const bool cloudy = valid && s_condition != WINDOW_WEATHER_CLEAR &&
                        s_condition != WINDOW_WEATHER_FOG;
    const bool rainy = valid && (s_condition == WINDOW_WEATHER_RAIN || s_condition == WINDOW_WEATHER_THUNDER);
    const bool snowy = valid && s_condition == WINDOW_WEATHER_SNOW;
    const bool foggy = valid && s_condition == WINDOW_WEATHER_FOG;
    /* 云缓慢漂移，出界后回到另一侧。 */
    if (cloudy) {
        for (int i = 0; i < CLOUD_COUNT; ++i) {
            lv_obj_t *cloud = s_clouds[i];
            if ((s_tick + i * 3) % 3 != 0) continue;
            int x = lv_obj_get_x(cloud) + 1;
            if (x > SCREEN + 20) x = -150;
            lv_obj_set_x(cloud, x);
        }
    }
    /* 雨滴快速下落。 */
    if (rainy) {
        for (int i = 0; i < RAIN_COUNT; ++i) {
            lv_obj_t *drop = s_rain[i];
            int y = lv_obj_get_y(drop) + 16 + (i % 3) * 4;
            if (y > GROUND_TOP) y = 70 + (int)(esp_random() % 40);
            lv_obj_set_y(drop, y);
        }
    }
    /* 雪花缓慢飘落并左右摇摆（±2 交替，无累积漂移）。 */
    if (snowy) {
        for (int i = 0; i < SNOW_COUNT; ++i) {
            lv_obj_t *flake = s_snow[i];
            int y = lv_obj_get_y(flake) + 3 + (i % 2);
            if (y > GROUND_TOP) y = 70 + (int)(esp_random() % 30);
            lv_obj_set_y(flake, y);
            if (s_tick % 4 == 0) {
                int x = lv_obj_get_x(flake) + ((s_tick / 4 + i) % 2 ? 2 : -2);
                lv_obj_set_x(flake, x);
            }
        }
    }
    /* 雾带水平缓慢流动。 */
    if (foggy) {
        for (int i = 0; i < 2; ++i) {
            if (s_tick % 5 != i) continue;
            int x = lv_obj_get_x(s_fog[i]) + 2;
            if (x > SCREEN) x = -260;
            lv_obj_set_x(s_fog[i], x);
        }
    }
    /* 星星闪烁（仅在可见的夜晚分组更新，隐藏时跳过）。 */
    if (s_tick % 8 == 0 && !lv_obj_has_flag(s_stars[0], LV_OBJ_FLAG_HIDDEN)) {
        for (int i = 0; i < STAR_COUNT; ++i) {
            lv_obj_set_style_opa(s_stars[i], (s_tick / 8 + i) % 3 ? LV_OPA_COVER : LV_OPA_40, 0);
        }
    }
    /* 跑腿小人：双腿交替摆动，沿地面来回跑动。 */
    if (s_codex_working) {
        const int leg = (s_tick % 8) < 4 ? 40 : -40;
        lv_obj_set_style_transform_angle(s_runner_leg_left, leg * 10, 0);
        lv_obj_set_style_transform_angle(s_runner_leg_right, -leg * 10, 0);
        const int span = 300;
        const int phase = (int)(s_tick / 2) % (span * 2);
        const int x = phase < span ? 70 + phase : 70 + span * 2 - phase;
        lv_obj_set_x(s_runner, x);
        lv_obj_set_style_transform_scale_x(s_runner, phase < span ? 256 : -256, 0);
    }
    /* 工厂烟：圆点上升、变大、淡出。 */
    if (s_bambu_printing) {
        for (int i = 0; i < SMOKE_COUNT; ++i) {
            const int phase = (int)(s_tick + i * 22) % 66;
            lv_obj_set_pos(s_smoke[i], 363 - phase / 6, 196 - phase);
            lv_obj_set_style_opa(s_smoke[i], (lv_opa_t)(200 - phase * 3), 0);
        }
    }
    /* 雷阵雨：偶尔一道闪电照亮天空，亮两帧后熄灭。 */
    if (s_weather_valid && s_condition == WINDOW_WEATHER_THUNDER) {
        if (s_flash_frames > 0) {
            --s_flash_frames;
            lv_obj_set_style_opa(s_flash, s_flash_frames > 0 ? LV_OPA_60 : LV_OPA_0, 0);
        } else if (--s_lightning_countdown <= 0) {
            s_lightning_countdown = 40 + (int)(esp_random() % 80);
            s_flash_frames = 2;
        }
    } else if (s_flash_frames > 0) {
        /* 天气从雷阵雨切换走时，立即熄灭未放完的闪电；平时不重复设置样式。 */
        s_flash_frames = 0;
        lv_obj_set_style_opa(s_flash, LV_OPA_0, 0);
    }
}

static void scene_timer(lv_timer_t *timer)
{
    (void)timer;
    if (s_screen == NULL || lv_screen_active() != s_screen) return;
    ++s_tick;
    animate_scene();
    if (s_tick % 10 == 0) {
        time_t now = time(NULL);
        struct tm local;
        localtime_r(&now, &local);
        const float hour = now > 86400 ? local.tm_hour + local.tm_min / 60.0f : 12.0f;
        update_sky(hour, daylight_permille(hour));
        update_info();
    }
}

static lv_obj_t *make_cloud(lv_obj_t *parent, int x, int y, uint32_t hex)
{
    lv_obj_t *cloud = lv_obj_create(parent);
    lv_obj_remove_style_all(cloud);
    lv_obj_set_size(cloud, 130, 60);
    lv_obj_set_pos(cloud, x, y);
    lv_obj_remove_flag(cloud, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_opa(cloud, LV_OPA_TRANSP, 0);
    lv_obj_t *left = make_shape(cloud, 44, 44, hex, LV_RADIUS_CIRCLE);
    lv_obj_set_pos(left, 8, 14);
    lv_obj_t *middle = make_shape(cloud, 58, 58, hex, LV_RADIUS_CIRCLE);
    lv_obj_set_pos(middle, 38, 0);
    lv_obj_t *right = make_shape(cloud, 40, 40, hex, LV_RADIUS_CIRCLE);
    lv_obj_set_pos(right, 84, 18);
    lv_obj_t *base = make_shape(cloud, 108, 24, hex, 12);
    lv_obj_set_pos(base, 10, 32);
    return cloud;
}

lv_obj_t *window_world_build(void)
{
    s_ui_font = ui_font_fixed_20;
    s_ui_font.fallback = &ui_font_detail_20;
    s_screen = lv_obj_create(NULL);
    lv_obj_remove_flag(s_screen, LV_OBJ_FLAG_SCROLLABLE);

    s_sky = make_shape(s_screen, SCREEN, SCREEN, 0x2E7CD6, 0);
    lv_obj_set_style_bg_grad_dir(s_sky, LV_GRAD_DIR_VER, 0);
    lv_obj_set_pos(s_sky, 0, 0);

    for (int i = 0; i < STAR_COUNT; ++i) {
        s_stars[i] = make_shape(s_screen, 3, 3, 0xF1F8F5, LV_RADIUS_CIRCLE);
        lv_obj_set_pos(s_stars[i], 40 + (int)(esp_random() % 380), 50 + (int)(esp_random() % 150));
    }
    s_sun = make_shape(s_screen, 34, 34, 0xFFD964, LV_RADIUS_CIRCLE);
    lv_obj_set_style_shadow_width(s_sun, 30, 0);
    lv_obj_set_style_shadow_color(s_sun, lv_color_hex(0xFFB13D), 0);
    lv_obj_set_style_shadow_opa(s_sun, LV_OPA_50, 0);
    s_moon = make_shape(s_screen, 28, 28, 0xE8E4D0, LV_RADIUS_CIRCLE);
    lv_obj_set_style_shadow_width(s_moon, 24, 0);
    lv_obj_set_style_shadow_color(s_moon, lv_color_hex(0xB9C4E0), 0);
    lv_obj_set_style_shadow_opa(s_moon, LV_OPA_40, 0);

    /* 远山与近丘：大圆的一部分露出地平线，形成柔和的山脊线。 */
    s_hill_far = make_shape(s_screen, 420, 420, 0x4A6B8A, LV_RADIUS_CIRCLE);
    lv_obj_set_pos(s_hill_far, -110, 236);
    s_hill_near = make_shape(s_screen, 460, 460, 0x3D7050, LV_RADIUS_CIRCLE);
    lv_obj_set_pos(s_hill_near, 180, 262);
    s_ground = make_shape(s_screen, SCREEN, SCREEN - GROUND_TOP, 0x2E5D43, 0);
    lv_obj_set_pos(s_ground, 0, GROUND_TOP);

    /* 远处工厂：Bambu 打印时烟囱冒烟、窗户亮灯。 */
    s_factory = make_shape(s_screen, 62, 40, 0x55606A, 3);
    lv_obj_set_pos(s_factory, 330, 290);
    s_factory_roof = make_shape(s_screen, 70, 10, 0x424C55, 2);
    lv_obj_set_pos(s_factory_roof, 326, 284);
    s_chimney = make_shape(s_screen, 12, 34, 0x424C55, 2);
    lv_obj_set_pos(s_chimney, 360, 258);
    s_factory_window = make_shape(s_screen, 14, 12, 0x2A3438, 2);
    lv_obj_set_pos(s_factory_window, 342, 302);
    for (int i = 0; i < SMOKE_COUNT; ++i) {
        s_smoke[i] = make_shape(s_screen, 14, 14, 0xC7D2CE, LV_RADIUS_CIRCLE);
        lv_obj_set_pos(s_smoke[i], 363, 196);
    }

    s_clouds[0] = make_cloud(s_screen, 30, 96, 0xF1F4F6);
    s_clouds[1] = make_cloud(s_screen, 250, 140, 0xE3E9EE);
    s_clouds[2] = make_cloud(s_screen, 120, 70, 0xD4DCE3);

    for (int i = 0; i < RAIN_COUNT; ++i) {
        s_rain[i] = make_shape(s_screen, 2, 12, 0x9CC8E8, 1);
        lv_obj_set_pos(s_rain[i], 50 + (int)(esp_random() % 370), 70 + (int)(esp_random() % 220));
        lv_obj_set_style_transform_angle(s_rain[i], 150, 0);
    }
    for (int i = 0; i < SNOW_COUNT; ++i) {
        s_snow[i] = make_shape(s_screen, 5, 5, 0xF1F8F5, LV_RADIUS_CIRCLE);
        lv_obj_set_pos(s_snow[i], 50 + (int)(esp_random() % 370), 70 + (int)(esp_random() % 200));
    }
    for (int i = 0; i < 2; ++i) {
        s_fog[i] = make_shape(s_screen, 260, 34, 0xD8DEE2, 17);
        lv_obj_set_style_opa(s_fog[i], LV_OPA_40, 0);
        lv_obj_set_pos(s_fog[i], i ? 160 : -60, 210 + i * 46);
    }

    /* 跑腿小人：Codex 工作时在窗前的地面上跑动。 */
    s_runner = lv_obj_create(s_screen);
    lv_obj_remove_style_all(s_runner);
    lv_obj_set_size(s_runner, 30, 44);
    lv_obj_set_pos(s_runner, 70, 340);
    lv_obj_remove_flag(s_runner, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *head = make_shape(s_runner, 12, 12, 0xFFB13D, LV_RADIUS_CIRCLE);
    lv_obj_set_pos(head, 9, 0);
    lv_obj_t *body = make_shape(s_runner, 8, 16, 0xFF9D3D, 3);
    lv_obj_set_pos(body, 11, 12);
    s_runner_leg_left = make_shape(s_runner, 4, 15, 0xFFB13D, 2);
    lv_obj_set_pos(s_runner_leg_left, 10, 28);
    lv_obj_set_style_transform_pivot_y(s_runner_leg_left, 0, 0);
    s_runner_leg_right = make_shape(s_runner, 4, 15, 0xE8862B, 2);
    lv_obj_set_pos(s_runner_leg_right, 17, 28);
    lv_obj_set_style_transform_pivot_y(s_runner_leg_right, 0, 0);
    lv_obj_set_style_transform_pivot_x(s_runner, 15, 0);

    /* 闪电：全屏白色一闪而过。 */
    s_flash = make_shape(s_screen, SCREEN, SCREEN, 0xFFFFFF, 0);
    lv_obj_set_style_opa(s_flash, LV_OPA_0, 0);

    s_title = make_label(s_screen, "窗外小世界", COLOR_TEXT);
    lv_obj_align(s_title, LV_ALIGN_TOP_MID, 0, 48);
    lv_obj_set_style_text_color(s_title, lv_color_hex(COLOR_TEXT), 0);
    s_clock = make_label(s_screen, "--:--", COLOR_TEXT);
    lv_obj_align(s_clock, LV_ALIGN_TOP_MID, 0, 78);
    lv_obj_set_style_opa(s_clock, LV_OPA_80, 0);
    s_info = make_label(s_screen, "昼夜流转 · 等待天气数据", COLOR_MUTED);
    /* 长城市名/天气描述截断为省略号，避免超出圆屏。 */
    lv_obj_set_width(s_info, 380);
    lv_label_set_long_mode(s_info, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_align(s_info, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_info, LV_ALIGN_BOTTOM_MID, 0, -58);

    lv_timer_create(scene_timer, 100, NULL);
    update_weather_visibility();
    update_sky(12.0f, 1000);
    update_info();
    return s_screen;
}

void window_world_apply(const codex_snapshot_t *snapshot)
{
    if (snapshot == NULL) return;
    s_weather_valid = snapshot->weather_valid;
    s_condition = snapshot->weather_condition;
    s_temperature_available = snapshot->weather_temperature_available;
    s_temperature = snapshot->weather_temperature;
    strlcpy(s_weather_text, snapshot->weather_text, sizeof(s_weather_text));
    strlcpy(s_city, snapshot->weather_city, sizeof(s_city));
    s_codex_working = snapshot->status == CODEX_STATUS_WORKING;
    s_bambu_printing = snapshot->bambu_status == BAMBU_STATUS_PRINTING ||
                       snapshot->bambu_status == BAMBU_STATUS_PREPARING;
    update_weather_visibility();
    update_info();
}
