#include "performance_monitor.h"

#include <float.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

LV_FONT_DECLARE(ui_font_fixed_20);
LV_FONT_DECLARE(ui_font_detail_20);

#define SCREEN_SIZE 466
#define GAUGE_COUNT 4
#define RATE_MIN_SCALE 1024
#define RATE_MAX_SCALE 1073741824
#define RATE_POINTER_MAX 1000

#define COLOR_BG 0x000000
#define COLOR_TEXT 0xF1F8F5
#define COLOR_MUTED 0x8E9B97
#define COLOR_TICK 0x59605E
#define COLOR_BLUE 0x5DA9FF
#define COLOR_VIOLET 0x9D7BFF
#define COLOR_WARNING 0xF2C66D
#define COLOR_CYAN 0x20D6E6

typedef enum {
    GAUGE_CPU = 0,
    GAUGE_MEMORY,
    GAUGE_DISK,
    GAUGE_NETWORK,
} gauge_type_t;

typedef struct {
    gauge_type_t type;
    uint32_t accent;
    lv_obj_t *container;
    lv_obj_t *scale;
    lv_obj_t *needle;
    lv_obj_t *hub;
    lv_obj_t *title;
    lv_obj_t *value;
    lv_obj_t *detail_first;
    lv_obj_t *detail_second;
    lv_point_precise_t needle_points[2];
    const char *axis_text[6];
    char axis_storage[5][16];
    int32_t rate_scale;
    uint8_t downshift_samples;
} performance_gauge_t;

static lv_obj_t *s_screen;
static lv_obj_t *s_safe;
static lv_obj_t *s_empty;
static lv_font_t s_ui_font;
static performance_gauge_t s_gauges[GAUGE_COUNT];
static uint8_t s_layout_mask;

static void set_hidden(lv_obj_t *object, bool hidden)
{
    if (hidden) lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
}

static lv_obj_t *make_label(lv_obj_t *parent, const char *text,
                            const lv_font_t *font, uint32_t text_color)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(text_color), 0);
    lv_obj_remove_flag(label, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    return label;
}

/* value 始终是 bytes/s；轴标签省略 /s，副文本使用紧凑单位以适配小仪表。 */
static void format_bytes(char *buffer, size_t size, double value,
                         bool compact, bool per_second)
{
    static const char *const units[] = {"B", "KB", "MB", "GB", "TB", "PB", "EB"};
    static const char *const compact_units[] = {"B", "K", "M", "G", "T", "P", "E"};
    size_t unit = 0;
    while (value >= 1024.0 && unit < 6) {
        value /= 1024.0;
        unit++;
    }
    char number[24];
    if (unit == 6 && value >= 1000000.0) snprintf(number, sizeof(number), "%.2e", value);
    else if (value < 10.0 && unit > 0) snprintf(number, sizeof(number), "%.1f", value);
    else snprintf(number, sizeof(number), "%.0f", value);
    snprintf(buffer, size, compact ? "%s%s%s" : "%s %s%s", number,
             compact ? compact_units[unit] : units[unit], per_second ? "/s" : "");
}

static double combined_rate(double first, double second)
{
    double total = first + second;
    return isfinite(total) ? total : DBL_MAX;
}

static void update_axis_labels(performance_gauge_t *gauge)
{
    if (gauge->type == GAUGE_CPU || gauge->type == GAUGE_MEMORY) {
        static const char *const labels[5] = {"0", "25", "50", "75", "100"};
        for (size_t index = 0; index < 5; ++index) {
            strlcpy(gauge->axis_storage[index], labels[index], sizeof(gauge->axis_storage[index]));
        }
    } else {
        for (size_t index = 0; index < 5; ++index) {
            double value = (double)gauge->rate_scale * (double)index / 4.0;
            format_bytes(gauge->axis_storage[index], sizeof(gauge->axis_storage[index]),
                         value, true, false);
        }
    }
    lv_scale_set_text_src(gauge->scale, gauge->axis_text);
    lv_obj_invalidate(gauge->scale);
}

static void set_rate_scale(performance_gauge_t *gauge, int32_t scale)
{
    if (gauge->rate_scale == scale) return;
    gauge->rate_scale = scale;
    update_axis_labels(gauge);
}

static void update_rate_scale(performance_gauge_t *gauge, double total)
{
    bool changed = false;
    while (gauge->rate_scale < RATE_MAX_SCALE &&
           total > (double)gauge->rate_scale * 0.80) {
        set_rate_scale(gauge, gauge->rate_scale * 4);
        changed = true;
    }
    if (changed) {
        gauge->downshift_samples = 0;
        return;
    }

    if (gauge->rate_scale > RATE_MIN_SCALE &&
        total <= (double)(gauge->rate_scale / 4) * 0.80) {
        if (gauge->downshift_samples < 3) gauge->downshift_samples++;
        if (gauge->downshift_samples >= 3) {
            set_rate_scale(gauge, gauge->rate_scale / 4);
            gauge->downshift_samples = 0;
        }
    } else {
        gauge->downshift_samples = 0;
    }
}

static void set_needle(performance_gauge_t *gauge, int32_t value, bool valid)
{
    int32_t length = lv_obj_get_width(gauge->scale) / 2 - 20;
    if (length < 20) length = 20;
    lv_scale_set_line_needle_value(gauge->scale, gauge->needle, length, value);
    lv_obj_set_style_line_color(gauge->needle,
                                lv_color_hex(valid ? gauge->accent : COLOR_TICK), 0);
    lv_obj_set_style_text_color(gauge->value,
                                lv_color_hex(valid ? COLOR_TEXT : COLOR_MUTED), 0);
}

static void apply_percent(performance_gauge_t *gauge, double percent, bool valid)
{
    if (!valid) {
        lv_label_set_text(gauge->value, "--");
        set_needle(gauge, 0, false);
        return;
    }
    lv_label_set_text_fmt(gauge->value, "%.0f%%", percent);
    int32_t pointer = (int32_t)(percent + 0.5);
    if (pointer < 0) pointer = 0;
    if (pointer > 100) pointer = 100;
    set_needle(gauge, pointer, true);
}

static void apply_rate(performance_gauge_t *gauge, double first, double second,
                       const char *first_caption, const char *second_caption, bool valid)
{
    char first_text[24];
    char second_text[24];
    if (!valid) {
        gauge->downshift_samples = 0;
        lv_label_set_text(gauge->value, "--");
        lv_label_set_text_fmt(gauge->detail_first, "%s--", first_caption);
        lv_label_set_text_fmt(gauge->detail_second, "%s--", second_caption);
        set_needle(gauge, 0, false);
        return;
    }

    double total = combined_rate(first, second);
    update_rate_scale(gauge, total);
    int32_t pointer = total >= (double)gauge->rate_scale ? RATE_POINTER_MAX :
        (int32_t)(total * RATE_POINTER_MAX / (double)gauge->rate_scale + 0.5);
    char total_text[32];
    format_bytes(total_text, sizeof(total_text), total, false, true);
    format_bytes(first_text, sizeof(first_text), first, true, true);
    format_bytes(second_text, sizeof(second_text), second, true, true);
    lv_label_set_text(gauge->value, total_text);
    lv_label_set_text_fmt(gauge->detail_first, "%s%s", first_caption, first_text);
    lv_label_set_text_fmt(gauge->detail_second, "%s%s", second_caption, second_text);
    set_needle(gauge, pointer, true);
}

static void layout_gauge(performance_gauge_t *gauge, int32_t center_x,
                         int32_t center_y, int32_t size)
{
    lv_obj_set_size(gauge->container, size, size);
    lv_obj_set_pos(gauge->container, center_x - size / 2, center_y - size / 2);
    lv_obj_set_size(gauge->scale, size, size);
    lv_obj_center(gauge->scale);

    bool compact = size <= 200;
    lv_obj_set_style_line_width(gauge->needle, size >= 300 ? 10 : size >= 200 ? 5 : 3, 0);
    int32_t hub_size = size >= 300 ? 30 : size >= 200 ? 16 : 11;
    lv_obj_set_size(gauge->hub, hub_size, hub_size);
    lv_obj_center(gauge->hub);

    lv_obj_set_width(gauge->title, size - 12);
    lv_obj_set_style_text_align(gauge->title, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_transform_scale(gauge->title, size >= 300 ? 384 : compact ? 200 : 256, 0);
    lv_obj_set_style_transform_pivot_x(gauge->title, (size - 12) / 2, 0);
    lv_obj_set_style_transform_pivot_y(gauge->title, 10, 0);
    lv_obj_align(gauge->title, LV_ALIGN_TOP_MID, 0, size * 17 / 100);

    lv_obj_set_width(gauge->value, size - 12);
    lv_obj_set_style_text_align(gauge->value, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_font(gauge->value,
                               size >= 200 ? &lv_font_montserrat_28 : &lv_font_montserrat_22, 0);
    lv_obj_set_style_transform_scale(gauge->value, size >= 300 ? 384 : 256, 0);
    lv_obj_set_style_transform_pivot_x(gauge->value, (size - 12) / 2, 0);
    lv_obj_set_style_transform_pivot_y(gauge->value, 16, 0);
    lv_obj_align(gauge->value, LV_ALIGN_CENTER, 0, size * 13 / 100);

    lv_obj_t *details[2] = {gauge->detail_first, gauge->detail_second};
    for (size_t index = 0; index < 2; ++index) {
        int32_t width = size / 2 - 4;
        lv_obj_set_size(details[index], width, 24);
        lv_obj_set_style_text_align(details[index], LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_transform_scale(details[index], compact ? 150 : 200, 0);
        lv_obj_set_style_transform_pivot_x(details[index], width / 2, 0);
        lv_obj_set_style_transform_pivot_y(details[index], 12, 0);
        lv_obj_align(details[index], LV_ALIGN_CENTER,
                     index == 0 ? -size * 20 / 100 : size * 20 / 100,
                     size * 32 / 100);
    }
    lv_scale_set_line_needle_value(gauge->scale, gauge->needle, size / 2 - 20, 0);
}

static void apply_layout(const codex_snapshot_t *snapshot)
{
    uint8_t mask = 0;
    if (snapshot != NULL && snapshot->valid) {
        if (snapshot->performance_cpu_enabled) mask |= 1U << GAUGE_CPU;
        if (snapshot->performance_memory_enabled) mask |= 1U << GAUGE_MEMORY;
        if (snapshot->performance_disk_enabled) mask |= 1U << GAUGE_DISK;
        if (snapshot->performance_network_enabled) mask |= 1U << GAUGE_NETWORK;
    }
    if (mask == s_layout_mask) return;
    s_layout_mask = mask;

    for (size_t index = 0; index < GAUGE_COUNT; ++index) {
        set_hidden(s_gauges[index].container, true);
    }
    set_hidden(s_empty, mask != 0);
    if (mask == 0) return;

    uint8_t slots[GAUGE_COUNT];
    uint8_t count = 0;
    for (uint8_t index = 0; index < GAUGE_COUNT; ++index) {
        if (mask & (1U << index)) slots[count++] = index;
    }

    int32_t size = count == 1 ? SCREEN_SIZE : count == 2 ? 224 : 200;
    for (uint8_t slot = 0; slot < count; ++slot) {
        int32_t center_x;
        int32_t center_y;
        if (count == 1) {
            center_x = SCREEN_SIZE / 2;
            center_y = SCREEN_SIZE / 2;
        } else if (count == 2) {
            center_x = slot == 0 ? 121 : 345;
            center_y = SCREEN_SIZE / 2;
        } else {
            center_x = count == 3 && slot == 2 ? SCREEN_SIZE / 2 : (slot % 2 == 0 ? 139 : 327);
            center_y = slot < 2 ? 139 : 327;
        }
        performance_gauge_t *gauge = &s_gauges[slots[slot]];
        layout_gauge(gauge, center_x, center_y, size);
        set_hidden(gauge->container, false);
    }
}

static void build_gauge(performance_gauge_t *gauge, gauge_type_t type,
                        const char *title, uint32_t accent)
{
    memset(gauge, 0, sizeof(*gauge));
    gauge->type = type;
    gauge->accent = accent;
    gauge->rate_scale = RATE_MIN_SCALE;
    gauge->container = lv_obj_create(s_safe);
    lv_obj_remove_style_all(gauge->container);
    lv_obj_remove_flag(gauge->container, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);

    gauge->scale = lv_scale_create(gauge->container);
    lv_scale_set_mode(gauge->scale, LV_SCALE_MODE_ROUND_INNER);
    lv_scale_set_total_tick_count(gauge->scale, 21);
    lv_scale_set_major_tick_every(gauge->scale, 5);
    lv_scale_set_label_show(gauge->scale, true);
    lv_scale_set_angle_range(gauge->scale, 270);
    lv_scale_set_rotation(gauge->scale, 135);
    lv_obj_set_style_bg_opa(gauge->scale, LV_OPA_TRANSP, 0);
    lv_obj_set_style_arc_color(gauge->scale, lv_color_hex(0x26302D), 0);
    lv_obj_set_style_arc_width(gauge->scale, 2, 0);
    lv_obj_set_style_line_color(gauge->scale, lv_color_hex(COLOR_TICK), LV_PART_ITEMS);
    lv_obj_set_style_line_width(gauge->scale, 1, LV_PART_ITEMS);
    lv_obj_set_style_length(gauge->scale, 4, LV_PART_ITEMS);
    lv_obj_set_style_line_color(gauge->scale, lv_color_hex(accent), LV_PART_INDICATOR);
    lv_obj_set_style_line_width(gauge->scale, 2, LV_PART_INDICATOR);
    lv_obj_set_style_length(gauge->scale, 9, LV_PART_INDICATOR);
    lv_obj_set_style_text_color(gauge->scale, lv_color_hex(COLOR_MUTED), LV_PART_INDICATOR);
    lv_obj_set_style_text_font(gauge->scale, &lv_font_montserrat_14, LV_PART_INDICATOR);
    lv_obj_remove_flag(gauge->scale, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);

    bool percent = type == GAUGE_CPU || type == GAUGE_MEMORY;
    lv_scale_set_range(gauge->scale, 0, percent ? 100 : RATE_POINTER_MAX);
    for (size_t index = 0; index < 5; ++index) gauge->axis_text[index] = gauge->axis_storage[index];
    gauge->axis_text[5] = NULL;

    gauge->needle = lv_line_create(gauge->scale);
    lv_obj_set_style_line_color(gauge->needle, lv_color_hex(accent), 0);
    lv_obj_set_style_line_rounded(gauge->needle, true, 0);
    lv_obj_remove_flag(gauge->needle, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_line_set_points_mutable(gauge->needle, gauge->needle_points, 2);

    gauge->hub = lv_obj_create(gauge->scale);
    lv_obj_remove_style_all(gauge->hub);
    lv_obj_set_style_radius(gauge->hub, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(gauge->hub, lv_color_hex(accent), 0);
    lv_obj_set_style_bg_opa(gauge->hub, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(gauge->hub, 2, 0);
    lv_obj_set_style_border_color(gauge->hub, lv_color_hex(COLOR_BG), 0);
    lv_obj_remove_flag(gauge->hub, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);

    gauge->title = make_label(gauge->scale, title, &s_ui_font, accent);
    lv_label_set_long_mode(gauge->title, LV_LABEL_LONG_DOT);
    gauge->value = make_label(gauge->scale, "--", &lv_font_montserrat_22, COLOR_MUTED);
    lv_label_set_long_mode(gauge->value, LV_LABEL_LONG_DOT);
    gauge->detail_first = make_label(gauge->scale, "--", &s_ui_font, COLOR_MUTED);
    gauge->detail_second = make_label(gauge->scale, "--", &s_ui_font, COLOR_MUTED);
    lv_label_set_long_mode(gauge->detail_first, LV_LABEL_LONG_DOT);
    lv_label_set_long_mode(gauge->detail_second, LV_LABEL_LONG_DOT);
    set_hidden(gauge->detail_first, percent);
    set_hidden(gauge->detail_second, percent);

    layout_gauge(gauge, SCREEN_SIZE / 2, SCREEN_SIZE / 2, 140);
    update_axis_labels(gauge);
    set_hidden(gauge->container, true);
}

lv_obj_t *performance_monitor_build(void)
{
    s_ui_font = ui_font_fixed_20;
    s_ui_font.fallback = &ui_font_detail_20;
    s_layout_mask = 0;
    s_screen = lv_obj_create(NULL);
    lv_obj_remove_flag(s_screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(s_screen, lv_color_hex(COLOR_BG), 0);
    lv_obj_set_style_bg_opa(s_screen, LV_OPA_COVER, 0);

    s_safe = lv_obj_create(s_screen);
    lv_obj_remove_style_all(s_safe);
    lv_obj_set_size(s_safe, SCREEN_SIZE, SCREEN_SIZE);
    lv_obj_center(s_safe);
    lv_obj_remove_flag(s_safe, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);

    s_empty = make_label(s_safe, "未选择指标", &s_ui_font, COLOR_MUTED);
    lv_obj_align(s_empty, LV_ALIGN_CENTER, 0, 0);

    build_gauge(&s_gauges[GAUGE_CPU], GAUGE_CPU, "CPU", COLOR_BLUE);
    build_gauge(&s_gauges[GAUGE_MEMORY], GAUGE_MEMORY, "内存", COLOR_VIOLET);
    build_gauge(&s_gauges[GAUGE_DISK], GAUGE_DISK, "磁盘", COLOR_WARNING);
    build_gauge(&s_gauges[GAUGE_NETWORK], GAUGE_NETWORK, "网络", COLOR_CYAN);
    return s_screen;
}

lv_obj_t *performance_monitor_content(void)
{
    return s_safe;
}

void performance_monitor_apply(const codex_snapshot_t *snapshot)
{
    if (s_screen == NULL) return;
    apply_layout(snapshot);
    bool source_valid = snapshot != NULL && snapshot->valid && snapshot->source_online &&
                        !snapshot->stale && snapshot->performance_connected &&
                        !snapshot->performance_stale;

    apply_percent(&s_gauges[GAUGE_CPU], snapshot != NULL ? snapshot->performance_cpu_percent : 0,
                  source_valid && snapshot->performance_cpu_available);
    apply_percent(&s_gauges[GAUGE_MEMORY], snapshot != NULL ? snapshot->performance_memory_percent : 0,
                  source_valid && snapshot->performance_memory_available);
    apply_rate(&s_gauges[GAUGE_DISK],
               snapshot != NULL ? snapshot->performance_disk_read_bytes_per_sec : 0,
               snapshot != NULL ? snapshot->performance_disk_write_bytes_per_sec : 0,
               "读", "写", source_valid && snapshot->performance_disk_available);
    apply_rate(&s_gauges[GAUGE_NETWORK],
               snapshot != NULL ? snapshot->performance_network_rx_bytes_per_sec : 0,
               snapshot != NULL ? snapshot->performance_network_tx_bytes_per_sec : 0,
               "收", "发", source_valid && snapshot->performance_network_available);
}
