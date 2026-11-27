#include "pomodoro.h"

#include <stdio.h>

LV_FONT_DECLARE(ui_font_chinese_semibold_24);
LV_FONT_DECLARE(ui_font_detail_20);
LV_FONT_DECLARE(ui_font_fixed_20);
LV_FONT_DECLARE(ui_font_digits_64);
LV_FONT_DECLARE(lv_font_montserrat_32);

#define SCREEN_SIZE 466
#define SAFE_SIZE 358
#define COLOR_TEXT 0xF8F1EF
#define COLOR_MUTED 0xC4B7B4
#define COLOR_TOMATO 0xFF6B5E
#define COLOR_TRACK 0x4A2723

#define POMODORO_MIN_MINUTES 1
#define POMODORO_MAX_MINUTES 120
#define POMODORO_DEFAULT_MAX 60

typedef enum {
    POMODORO_SETUP,
    POMODORO_RUNNING,
    POMODORO_FINISHED,
} pomodoro_phase_t;

static lv_obj_t *s_screen;
static lv_obj_t *s_content;
static lv_obj_t *s_arc;
static lv_obj_t *s_value;
static lv_obj_t *s_caption;
static lv_obj_t *s_start_button;
static lv_obj_t *s_start_label;
static lv_obj_t *s_overlay;
static lv_font_t s_title_font;
static lv_font_t s_body_font;

static pomodoro_phase_t s_phase;
static uint32_t s_max_minutes = POMODORO_DEFAULT_MAX;
static uint32_t s_minutes = 25;
static uint32_t s_total_seconds;
static uint32_t s_started_tick;
static bool s_adjusting;
static uint32_t s_last_gesture_at;
static void (*s_finish_callback)(void);

static lv_obj_t *make_label(lv_obj_t *parent, const char *text, const lv_font_t *font,
                            uint32_t text_color)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(text_color), 0);
    lv_obj_remove_flag(label, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    return label;
}

static uint32_t remaining_seconds(void)
{
    uint32_t elapsed = lv_tick_elaps(s_started_tick) / 1000;
    return elapsed >= s_total_seconds ? 0 : s_total_seconds - elapsed;
}

static void render_setup(void)
{
    lv_arc_set_mode(s_arc, LV_ARC_MODE_NORMAL);
    lv_arc_set_range(s_arc, POMODORO_MIN_MINUTES, (int32_t)s_max_minutes);
    lv_arc_set_value(s_arc, (int32_t)s_minutes);
    lv_obj_add_flag(s_arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_bg_opa(s_arc, LV_OPA_COVER, LV_PART_KNOB);
    lv_obj_set_style_text_font(s_value, &ui_font_digits_64, 0);
    char text[8];
    snprintf(text, sizeof(text), "%lu", (unsigned long)s_minutes);
    lv_label_set_text(s_value, text);
    lv_label_set_text(s_caption, "分钟");
    lv_label_set_text(s_start_label, "开始");
    lv_obj_remove_flag(s_start_button, LV_OBJ_FLAG_HIDDEN);
}

static void render_running(void)
{
    uint32_t remaining = remaining_seconds();
    lv_arc_set_range(s_arc, 0, (int32_t)s_total_seconds);
    lv_arc_set_value(s_arc, (int32_t)remaining);
    lv_obj_remove_flag(s_arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_bg_opa(s_arc, LV_OPA_TRANSP, LV_PART_KNOB);
    lv_obj_set_style_text_font(s_value, &lv_font_montserrat_32, 0);
    char text[12];
    snprintf(text, sizeof(text), "%02lu:%02lu", (unsigned long)(remaining / 60),
             (unsigned long)(remaining % 60));
    lv_label_set_text(s_value, text);
    lv_label_set_text(s_caption, "剩余时间");
    lv_label_set_text(s_start_label, "取消");
    lv_obj_remove_flag(s_start_button, LV_OBJ_FLAG_HIDDEN);
}

static void overlay_opa_anim(void *var, int32_t value)
{
    lv_obj_set_style_bg_opa((lv_obj_t *)var, (lv_opa_t)value, 0);
}

static void enter_finished(void)
{
    s_phase = POMODORO_FINISHED;
    /* 覆盖层不拦截点击：表盘与按钮先失去交互，点按落在屏幕本体上触发重置。 */
    lv_obj_remove_flag(s_arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(s_start_button, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_style_bg_opa(s_overlay, LV_OPA_COVER, 0);
    lv_anim_t anim;
    lv_anim_init(&anim);
    lv_anim_set_var(&anim, s_overlay);
    lv_anim_set_exec_cb(&anim, overlay_opa_anim);
    lv_anim_set_values(&anim, LV_OPA_40, LV_OPA_COVER);
    lv_anim_set_time(&anim, 600);
    lv_anim_set_playback_time(&anim, 600);
    lv_anim_set_repeat_count(&anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&anim);
    if (s_finish_callback != NULL) s_finish_callback();
}

static void reset_to_setup(void)
{
    lv_anim_delete(s_overlay, NULL);
    lv_obj_add_flag(s_overlay, LV_OBJ_FLAG_HIDDEN);
    s_phase = POMODORO_SETUP;
    if (s_minutes > s_max_minutes) s_minutes = s_max_minutes;
    if (s_minutes < POMODORO_MIN_MINUTES) s_minutes = POMODORO_MIN_MINUTES;
    render_setup();
}

static void gesture_event(lv_event_t *event)
{
    (void)event;
    s_last_gesture_at = lv_tick_get();
}

static void arc_event(lv_event_t *event)
{
    lv_event_code_t code = lv_event_get_code(event);
    if (code == LV_EVENT_PRESSED) {
        if (s_phase == POMODORO_SETUP) s_adjusting = true;
    } else if (code == LV_EVENT_VALUE_CHANGED) {
        if (s_phase != POMODORO_SETUP) return;
        s_minutes = (uint32_t)lv_arc_get_value(s_arc);
        char text[8];
        snprintf(text, sizeof(text), "%lu", (unsigned long)s_minutes);
        lv_label_set_text(s_value, text);
    } else if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST) {
        s_adjusting = false;
    }
}

static void start_clicked(lv_event_t *event)
{
    if (lv_event_get_code(event) != LV_EVENT_CLICKED) return;
    if (s_last_gesture_at != 0 && lv_tick_elaps(s_last_gesture_at) < 350) return;
    if (s_phase == POMODORO_SETUP) {
        s_total_seconds = s_minutes * 60;
        s_started_tick = lv_tick_get();
        s_phase = POMODORO_RUNNING;
        render_running();
    } else if (s_phase == POMODORO_RUNNING) {
        reset_to_setup();
    }
}

lv_obj_t *pomodoro_build(void)
{
    s_title_font = ui_font_chinese_semibold_24;
    s_title_font.fallback = &ui_font_detail_20;
    s_body_font = ui_font_fixed_20;
    s_body_font.fallback = &ui_font_detail_20;

    s_screen = lv_obj_create(NULL);
    lv_obj_remove_flag(s_screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(s_screen, gesture_event, LV_EVENT_GESTURE, NULL);
    lv_obj_set_style_bg_color(s_screen, lv_color_hex(0x1C100E), 0);
    lv_obj_set_style_bg_grad_color(s_screen, lv_color_hex(0x452420), 0);
    lv_obj_set_style_bg_grad_dir(s_screen, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(s_screen, LV_OPA_COVER, 0);

    s_content = lv_obj_create(s_screen);
    lv_obj_remove_style_all(s_content);
    lv_obj_set_size(s_content, SAFE_SIZE, SAFE_SIZE);
    lv_obj_center(s_content);
    lv_obj_remove_flag(s_content, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *title = make_label(s_content, "番茄钟", &s_title_font, COLOR_TEXT);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 0);

    s_arc = lv_arc_create(s_content);
    lv_obj_set_size(s_arc, 280, 280);
    lv_obj_align(s_arc, LV_ALIGN_TOP_MID, 0, 30);
    lv_arc_set_rotation(s_arc, 0);
    lv_arc_set_bg_angles(s_arc, 135, 405);
    lv_arc_set_mode(s_arc, LV_ARC_MODE_NORMAL);
    lv_obj_set_style_arc_width(s_arc, 14, LV_PART_MAIN);
    lv_obj_set_style_arc_width(s_arc, 14, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(s_arc, lv_color_hex(COLOR_TRACK), LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_arc, lv_color_hex(COLOR_TOMATO), LV_PART_INDICATOR);
    lv_obj_set_style_arc_opa(s_arc, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_arc_opa(s_arc, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s_arc, lv_color_hex(COLOR_TOMATO), LV_PART_KNOB);
    lv_obj_set_style_bg_opa(s_arc, LV_OPA_COVER, LV_PART_KNOB);
    lv_obj_set_style_radius(s_arc, LV_RADIUS_CIRCLE, LV_PART_KNOB);
    lv_obj_set_style_width(s_arc, 22, LV_PART_KNOB);
    lv_obj_set_style_height(s_arc, 22, LV_PART_KNOB);
    /* 沿表盘拖动旋钮属于设值手势，不冒泡成翻页滑动（同转盘页进度条）。 */
    lv_obj_remove_flag(s_arc, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_GESTURE_BUBBLE);
    lv_obj_add_event_cb(s_arc, arc_event, LV_EVENT_ALL, NULL);

    s_value = make_label(s_arc, "25", &ui_font_digits_64, COLOR_TEXT);
    lv_obj_align(s_value, LV_ALIGN_CENTER, 0, -14);
    s_caption = make_label(s_arc, "分钟", &s_body_font, COLOR_MUTED);
    lv_obj_align(s_caption, LV_ALIGN_CENTER, 0, 36);

    s_start_button = lv_button_create(s_content);
    lv_obj_set_size(s_start_button, 132, 46);
    lv_obj_align(s_start_button, LV_ALIGN_TOP_MID, 0, 286);
    lv_obj_set_style_radius(s_start_button, 23, 0);
    lv_obj_set_style_bg_color(s_start_button, lv_color_hex(COLOR_TOMATO), 0);
    lv_obj_set_style_bg_opa(s_start_button, LV_OPA_COVER, 0);
    lv_obj_set_style_shadow_width(s_start_button, 0, 0);
    lv_obj_remove_flag(s_start_button, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(s_start_button, start_clicked, LV_EVENT_CLICKED, NULL);
    s_start_label = make_label(s_start_button, "开始", &s_body_font, 0x1C100E);
    lv_obj_center(s_start_label);

    s_overlay = lv_obj_create(s_screen);
    lv_obj_remove_style_all(s_overlay);
    lv_obj_set_size(s_overlay, SCREEN_SIZE, SCREEN_SIZE);
    lv_obj_center(s_overlay);
    lv_obj_set_style_bg_color(s_overlay, lv_color_hex(0xC8372D), 0);
    lv_obj_set_style_bg_opa(s_overlay, LV_OPA_COVER, 0);
    lv_obj_remove_flag(s_overlay, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *alert = make_label(s_overlay, "时间到", &s_title_font, 0xFFFFFF);
    lv_obj_set_style_transform_scale(alert, 512, 0);
    lv_obj_align(alert, LV_ALIGN_CENTER, 0, -36);
    lv_obj_t *hint = make_label(s_overlay, "点按屏幕重置", &s_body_font, 0xFFE3DF);
    lv_obj_align(hint, LV_ALIGN_CENTER, 0, 48);

    render_setup();
    return s_screen;
}

lv_obj_t *pomodoro_content(void)
{
    return s_content;
}

void pomodoro_apply(const codex_snapshot_t *snapshot)
{
    if (s_screen == NULL || snapshot == NULL) return;
    uint32_t max = snapshot->pomodoro_max_minutes;
    if (max < POMODORO_MIN_MINUTES || max > POMODORO_MAX_MINUTES) max = POMODORO_DEFAULT_MAX;
    s_max_minutes = max;
    if (!snapshot->pomodoro_enabled) {
        if (s_phase != POMODORO_SETUP) {
            reset_to_setup();
        } else if (s_minutes > s_max_minutes) {
            s_minutes = s_max_minutes;
            if (!s_adjusting) render_setup();
        }
        return;
    }
    if (s_phase == POMODORO_SETUP) {
        if (s_minutes > s_max_minutes) s_minutes = s_max_minutes;
        /* 用户正在拖动旋钮时不要用快照覆盖表盘值。 */
        if (!s_adjusting) render_setup();
    } else if (s_phase == POMODORO_RUNNING) {
        if (s_total_seconds > s_max_minutes * 60) s_total_seconds = s_max_minutes * 60;
        if (lv_screen_active() == s_screen) render_running();
    }
}

void pomodoro_tick(void)
{
    if (s_screen == NULL || s_phase != POMODORO_RUNNING) return;
    /* 计时全局运行：不在番茄钟页也要判定到点，回调负责亮屏跳页。 */
    if (remaining_seconds() == 0) {
        enter_finished();
        return;
    }
    if (lv_screen_active() == s_screen) render_running();
}

void pomodoro_set_finish_callback(void (*callback)(void))
{
    s_finish_callback = callback;
}

void pomodoro_screen_clicked(void)
{
    if (s_phase == POMODORO_FINISHED) reset_to_setup();
}