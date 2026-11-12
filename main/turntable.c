#include "turntable.h"

#include <stdio.h>
#include <string.h>

#include "connectivity.h"

LV_FONT_DECLARE(ui_font_chinese_semibold_24);
LV_FONT_DECLARE(ui_font_detail_20);
LV_FONT_DECLARE(ui_font_fixed_20);
LV_FONT_DECLARE(lv_font_montserrat_20);

#define SCREEN_SIZE 466
#define SAFE_SIZE 358
#define COLOR_TEXT 0xF1F8F5
#define COLOR_MUTED 0xB7C4BF
#define COLOR_TRACK 0x3D4945

static lv_obj_t *s_screen;
static lv_obj_t *s_content;
static lv_obj_t *s_disc;
static lv_obj_t *s_disc_center;
static lv_obj_t *s_grooves[3];
static lv_obj_t *s_status;
static lv_obj_t *s_title;
static lv_obj_t *s_detail;
static lv_obj_t *s_progress;
static lv_obj_t *s_elapsed;
static lv_obj_t *s_duration;
static lv_obj_t *s_buttons[3];
static lv_obj_t *s_playpause_icon;
static lv_font_t s_title_font;
static lv_font_t s_body_font;

static uint32_t s_base_progress_ms;
static uint32_t s_duration_ms;
static uint32_t s_received_tick;
static uint32_t s_track_revision;
static uint32_t s_seek_revision;
static bool s_playing;
static bool s_can_control;
static bool s_seeking;
static uint32_t s_last_gesture_at;

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

static lv_obj_t *make_circle(lv_obj_t *parent, int size, uint32_t fill)
{
    lv_obj_t *circle = lv_obj_create(parent);
    lv_obj_remove_style_all(circle);
    lv_obj_set_size(circle, size, size);
    lv_obj_set_style_radius(circle, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(circle, lv_color_hex(fill), 0);
    lv_obj_set_style_bg_opa(circle, LV_OPA_COVER, 0);
    lv_obj_remove_flag(circle, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    return circle;
}

static void format_time(uint32_t milliseconds, char *output, size_t capacity)
{
    uint32_t seconds = milliseconds / 1000;
    uint32_t hours = seconds / 3600;
    uint32_t minutes = (seconds / 60) % 60;
    seconds %= 60;
    if (hours > 0) {
        snprintf(output, capacity, "%lu:%02lu:%02lu", (unsigned long)hours,
                 (unsigned long)minutes, (unsigned long)seconds);
    } else {
        snprintf(output, capacity, "%02lu:%02lu", (unsigned long)minutes,
                 (unsigned long)seconds);
    }
}

static uint32_t displayed_progress(void)
{
    uint64_t position = s_base_progress_ms;
    if (s_playing) position += lv_tick_elaps(s_received_tick);
    if (s_duration_ms > 0 && position > s_duration_ms) position = s_duration_ms;
    if (position > UINT32_MAX) position = UINT32_MAX;
    return (uint32_t)position;
}

static void render_progress(void)
{
    uint32_t position;
    if (s_seeking) {
        position = (uint32_t)((uint64_t)s_duration_ms * lv_slider_get_value(s_progress) / 1000);
    } else {
        position = displayed_progress();
        uint32_t value = s_duration_ms > 0 ?
            (uint32_t)((uint64_t)position * 1000 / s_duration_ms) : 0;
        lv_slider_set_value(s_progress, (int32_t)value, LV_ANIM_OFF);
    }
    char elapsed[16];
    char duration[16];
    format_time(position, elapsed, sizeof(elapsed));
    format_time(s_duration_ms, duration, sizeof(duration));
    lv_label_set_text(s_elapsed, elapsed);
    lv_label_set_text(s_duration, duration);
}

static void render_control_state(void)
{
    lv_label_set_text(s_playpause_icon, s_playing ? LV_SYMBOL_PAUSE : LV_SYMBOL_PLAY);
    for (size_t index = 0; index < 3; index++) {
        if (s_can_control) lv_obj_remove_state(s_buttons[index], LV_STATE_DISABLED);
        else lv_obj_add_state(s_buttons[index], LV_STATE_DISABLED);
    }
}

static void gesture_event(lv_event_t *event)
{
    (void)event;
    s_last_gesture_at = lv_tick_get();
}

static void control_event(lv_event_t *event)
{
    if (lv_event_get_code(event) != LV_EVENT_CLICKED || !s_can_control) return;
    if (s_last_gesture_at != 0 && lv_tick_elaps(s_last_gesture_at) < 350) return;
    const char *action = lv_event_get_user_data(event);
    if (!connectivity_turntable_command(action, 0)) {
        lv_label_set_text(s_status, "指令发送失败");
        return;
    }
    lv_label_set_text(s_status, "指令已发送");
    if (strcmp(action, "playpause") == 0) {
        s_playing = !s_playing;
        s_received_tick = lv_tick_get();
        render_control_state();
    }
}

static void progress_event(lv_event_t *event)
{
    lv_event_code_t code = lv_event_get_code(event);
    if (code == LV_EVENT_PRESSED) {
        if (!s_can_control || s_duration_ms == 0) return;
        s_seeking = true;
        s_seek_revision = s_track_revision;
    } else if (code == LV_EVENT_VALUE_CHANGED) {
        if (s_seeking) render_progress();
    } else if (code == LV_EVENT_RELEASED) {
        if (!s_seeking) return;
        uint32_t position = (uint32_t)((uint64_t)s_duration_ms * lv_slider_get_value(s_progress) / 1000);
        bool same_track = s_seek_revision == s_track_revision;
        s_seeking = false;
        if (same_track && s_can_control && connectivity_turntable_command("seek", position)) {
            s_base_progress_ms = position;
            s_received_tick = lv_tick_get();
            lv_label_set_text(s_status, "正在调整进度");
        } else if (same_track) {
            lv_label_set_text(s_status, "指令发送失败");
        }
        render_progress();
    } else if (code == LV_EVENT_PRESS_LOST) {
        s_seeking = false;
        render_progress();
    }
}

static void apply_palette(const codex_snapshot_t *snapshot)
{
    static uint32_t applied[3];
    static bool initialized;
    if (initialized && memcmp(applied, snapshot->turntable_palette, sizeof(applied)) == 0) return;
    memcpy(applied, snapshot->turntable_palette, sizeof(applied));
    initialized = true;

    lv_obj_set_style_bg_color(s_screen, lv_color_hex(applied[0]), 0);
    lv_obj_set_style_bg_grad_color(s_screen, lv_color_hex(applied[2]), 0);
    lv_obj_set_style_bg_grad_dir(s_screen, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_color(s_disc, lv_color_hex(applied[2]), 0);
    lv_obj_set_style_border_color(s_disc, lv_color_hex(applied[1]), 0);
    lv_obj_set_style_bg_color(s_disc_center, lv_color_hex(applied[1]), 0);
    lv_obj_set_style_bg_color(s_progress, lv_color_hex(COLOR_TRACK), LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_progress, lv_color_hex(applied[1]), LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s_progress, lv_color_hex(applied[1]), LV_PART_KNOB);
    for (size_t index = 0; index < 3; index++) {
        lv_obj_set_style_border_color(s_grooves[index], lv_color_hex(applied[1]), 0);
        lv_obj_set_style_bg_color(s_buttons[index], lv_color_hex(applied[2]), 0);
        lv_obj_set_style_border_color(s_buttons[index], lv_color_hex(applied[1]), 0);
    }
}

lv_obj_t *turntable_build(void)
{
    s_title_font = ui_font_chinese_semibold_24;
    s_title_font.fallback = &ui_font_detail_20;
    s_body_font = ui_font_fixed_20;
    s_body_font.fallback = &ui_font_detail_20;

    s_screen = lv_obj_create(NULL);
    lv_obj_remove_flag(s_screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(s_screen, gesture_event, LV_EVENT_GESTURE, NULL);
    lv_obj_set_style_bg_color(s_screen, lv_color_hex(0x101C1A), 0);
    lv_obj_set_style_bg_grad_color(s_screen, lv_color_hex(0x28453D), 0);
    lv_obj_set_style_bg_grad_dir(s_screen, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(s_screen, LV_OPA_COVER, 0);

    s_content = lv_obj_create(s_screen);
    lv_obj_remove_style_all(s_content);
    lv_obj_set_size(s_content, SAFE_SIZE, SAFE_SIZE);
    lv_obj_center(s_content);
    lv_obj_remove_flag(s_content, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);

    s_status = make_label(s_content, "等待 Spotify", &s_body_font, COLOR_MUTED);
    lv_label_set_long_mode(s_status, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s_status, 310);
    lv_obj_set_style_text_align(s_status, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_status, LV_ALIGN_TOP_MID, 0, 0);

    s_disc = make_circle(s_content, 110, 0x28453D);
    lv_obj_set_pos(s_disc, 124, 40);
    lv_obj_set_style_border_width(s_disc, 2, 0);
    lv_obj_set_style_border_color(s_disc, lv_color_hex(0x5DA9FF), 0);
    const int groove_sizes[3] = {92, 74, 56};
    for (size_t index = 0; index < 3; index++) {
        s_grooves[index] = make_circle(s_disc, groove_sizes[index], 0x000000);
        lv_obj_set_style_bg_opa(s_grooves[index], LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(s_grooves[index], 1, 0);
        lv_obj_set_style_border_opa(s_grooves[index], LV_OPA_40, 0);
        lv_obj_center(s_grooves[index]);
    }
    s_disc_center = make_circle(s_disc, 40, 0x5DA9FF);
    lv_obj_center(s_disc_center);
    lv_obj_t *spindle = make_circle(s_disc_center, 8, 0x101C1A);
    lv_obj_center(spindle);

    s_title = make_label(s_content, "暂无播放", &s_title_font, COLOR_TEXT);
    lv_label_set_long_mode(s_title, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s_title, 320);
    lv_obj_set_style_text_align(s_title, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_title, LV_ALIGN_TOP_MID, 0, 158);

    s_detail = make_label(s_content, "在 Mac 上打开 Spotify", &s_body_font, COLOR_MUTED);
    lv_label_set_long_mode(s_detail, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s_detail, 310);
    lv_obj_set_style_text_align(s_detail, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_detail, LV_ALIGN_TOP_MID, 0, 190);

    const char *symbols[3] = {LV_SYMBOL_PREV, LV_SYMBOL_PLAY, LV_SYMBOL_NEXT};
    const char *actions[3] = {"previous", "playpause", "next"};
    const int positions[3] = {99, 153, 217};
    for (size_t index = 0; index < 3; index++) {
        int size = index == 1 ? 52 : 42;
        s_buttons[index] = lv_button_create(s_content);
        lv_obj_set_size(s_buttons[index], size, size);
        lv_obj_set_pos(s_buttons[index], positions[index], index == 1 ? 216 : 221);
        lv_obj_set_style_radius(s_buttons[index], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(s_buttons[index], lv_color_hex(0x28453D), 0);
        lv_obj_set_style_bg_opa(s_buttons[index], LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(s_buttons[index], 1, 0);
        lv_obj_set_style_border_color(s_buttons[index], lv_color_hex(0x5DA9FF), 0);
        lv_obj_set_style_shadow_width(s_buttons[index], 0, 0);
        lv_obj_set_style_opa(s_buttons[index], LV_OPA_40, LV_STATE_DISABLED);
        lv_obj_remove_flag(s_buttons[index], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_event_cb(s_buttons[index], control_event, LV_EVENT_CLICKED, (void *)actions[index]);
        lv_obj_t *icon = make_label(s_buttons[index], symbols[index], &lv_font_montserrat_20, COLOR_TEXT);
        lv_obj_center(icon);
        if (index == 1) s_playpause_icon = icon;
    }

    s_progress = lv_slider_create(s_content);
    lv_obj_set_size(s_progress, 260, 12);
    lv_obj_align(s_progress, LV_ALIGN_TOP_MID, 0, 278);
    lv_slider_set_range(s_progress, 0, 1000);
    lv_obj_set_style_radius(s_progress, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_radius(s_progress, LV_RADIUS_CIRCLE, LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_progress, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s_progress, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s_progress, lv_color_hex(0x5DA9FF), LV_PART_KNOB);
    lv_obj_set_style_bg_opa(s_progress, LV_OPA_COVER, LV_PART_KNOB);
    lv_obj_set_style_radius(s_progress, LV_RADIUS_CIRCLE, LV_PART_KNOB);
    lv_obj_set_style_width(s_progress, 16, LV_PART_KNOB);
    lv_obj_set_style_height(s_progress, 16, LV_PART_KNOB);
    lv_obj_remove_flag(s_progress, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_GESTURE_BUBBLE);
    lv_obj_add_event_cb(s_progress, progress_event, LV_EVENT_ALL, NULL);

    s_elapsed = make_label(s_content, "00:00", &s_body_font, COLOR_MUTED);
    lv_obj_align(s_elapsed, LV_ALIGN_TOP_LEFT, 48, 299);
    s_duration = make_label(s_content, "00:00", &s_body_font, COLOR_MUTED);
    lv_obj_align(s_duration, LV_ALIGN_TOP_RIGHT, -48, 299);
    render_control_state();
    return s_screen;
}

lv_obj_t *turntable_content(void)
{
    return s_content;
}

void turntable_apply(const codex_snapshot_t *snapshot)
{
    if (s_screen == NULL || snapshot == NULL) return;
    s_can_control = snapshot->source_online && !snapshot->stale && snapshot->turntable_connected &&
                    !snapshot->turntable_stale && snapshot->turntable_spotify_running &&
                    snapshot->turntable_playback_state != TURNTABLE_PLAYBACK_NOT_RUNNING &&
                    snapshot->turntable_playback_state != TURNTABLE_PLAYBACK_ERROR;
    if (s_track_revision != snapshot->turntable_track_revision || !s_can_control) s_seeking = false;
    s_track_revision = snapshot->turntable_track_revision;
    if (!s_seeking) {
        s_base_progress_ms = snapshot->turntable_progress_ms;
        s_duration_ms = snapshot->turntable_duration_ms;
        s_received_tick = lv_tick_get();
    }
    s_playing = snapshot->turntable_playback_state == TURNTABLE_PLAYBACK_PLAYING && s_can_control;
    apply_palette(snapshot);
    render_control_state();

    const char *title = snapshot->turntable_title[0] != '\0' ?
        snapshot->turntable_title : "暂无播放";
    lv_label_set_text(s_title, title);
    char detail[TURNTABLE_ARTIST_MAX + TURNTABLE_ALBUM_MAX + 4];
    if (snapshot->turntable_artist[0] != '\0' && snapshot->turntable_album[0] != '\0') {
        snprintf(detail, sizeof(detail), "%s · %s", snapshot->turntable_artist,
                 snapshot->turntable_album);
    } else if (snapshot->turntable_artist[0] != '\0') {
        strlcpy(detail, snapshot->turntable_artist, sizeof(detail));
    } else if (snapshot->turntable_album[0] != '\0') {
        strlcpy(detail, snapshot->turntable_album, sizeof(detail));
    } else {
        strlcpy(detail, "在 Mac 上打开 Spotify", sizeof(detail));
    }
    lv_label_set_text(s_detail, detail);

    const char *status = "正在播放";
    if (!snapshot->source_online || snapshot->stale || snapshot->turntable_stale) {
        status = "等待管理中心";
    } else if (snapshot->turntable_playback_state == TURNTABLE_PLAYBACK_ERROR) {
        status = snapshot->turntable_error[0] != '\0' ? snapshot->turntable_error : "Spotify 读取失败";
    } else if (!snapshot->turntable_spotify_running ||
               snapshot->turntable_playback_state == TURNTABLE_PLAYBACK_NOT_RUNNING) {
        status = "Spotify 未运行";
    } else if (snapshot->turntable_playback_state == TURNTABLE_PLAYBACK_PAUSED) {
        status = "已暂停";
    } else if (snapshot->turntable_playback_state == TURNTABLE_PLAYBACK_STOPPED) {
        status = "等待播放";
    }
    lv_label_set_text(s_status, status);
    render_progress();
}

void turntable_tick(void)
{
    if (s_screen == NULL) return;
    if (lv_screen_active() != s_screen) {
        if (s_seeking) {
            s_seeking = false;
            render_progress();
        }
        return;
    }
    render_progress();
}
