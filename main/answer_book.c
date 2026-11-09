#include "answer_book.h"

#include <stdint.h>

#include "esp_random.h"

LV_FONT_DECLARE(ui_font_fixed_20);
LV_FONT_DECLARE(ui_font_detail_20);

#define COLOR_TEXT 0xF1F8F5
#define COLOR_MUTED 0x8E9B97
#define COLOR_WARM 0xF2C66D
#define FLIP_DURATION_MS 700

static lv_obj_t *s_screen;
static lv_obj_t *s_card;
static lv_obj_t *s_answer;
static lv_obj_t *s_hint;
static lv_font_t s_answer_font;
static size_t s_last_index = (size_t)-1;
static size_t s_pending_index;
static uint32_t s_shuffle_step;
static bool s_busy;

static lv_obj_t *make_label(lv_obj_t *parent, const char *text, uint32_t text_color)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, &s_answer_font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(text_color), 0);
    return label;
}

static size_t next_answer_index(void)
{
    const size_t count = answer_book_answer_count();
    size_t index = esp_random() % count;
    if (count > 1 && index == s_last_index) {
        index = (index + 1 + esp_random() % (count - 1)) % count;
    }
    return index;
}

static void flip_anim_exec(void *var, int32_t value)
{
    (void)var;
    const int32_t half = FLIP_DURATION_MS / 2;
    const int32_t scale = value <= half ?
        256 - 232 * value / half :
        24 + 232 * (value - half) / half;
    lv_obj_set_style_transform_scale_x(s_card, scale, 0);

    const uint32_t step = (uint32_t)value / 70U;
    if (step != s_shuffle_step) {
        s_shuffle_step = step;
        lv_label_set_text(s_answer,
                          answer_book_answer_at(esp_random() % answer_book_answer_count()));
    }
}

static void flip_anim_completed(lv_anim_t *animation)
{
    (void)animation;
    lv_obj_set_style_transform_scale_x(s_card, 256, 0);
    lv_label_set_text(s_answer, answer_book_answer_at(s_pending_index));
    lv_obj_set_style_text_color(s_answer, lv_color_hex(COLOR_TEXT), 0);
    lv_label_set_text(s_hint, "再点按一次，换个答案");
    s_last_index = s_pending_index;
    s_busy = false;
}

void answer_book_activate(void)
{
    if (s_busy || s_card == NULL) return;
    s_pending_index = next_answer_index();
    s_shuffle_step = UINT32_MAX;
    s_busy = true;
    lv_obj_set_style_text_color(s_answer, lv_color_hex(COLOR_WARM), 0);
    lv_label_set_text(s_hint, "正在翻阅…");

    lv_anim_t animation;
    lv_anim_init(&animation);
    lv_anim_set_var(&animation, s_card);
    lv_anim_set_values(&animation, 0, FLIP_DURATION_MS);
    lv_anim_set_duration(&animation, FLIP_DURATION_MS);
    lv_anim_set_exec_cb(&animation, flip_anim_exec);
    lv_anim_set_completed_cb(&animation, flip_anim_completed);
    lv_anim_start(&animation);
}

static void answer_book_reset(void)
{
    if (s_card == NULL) return;
    lv_anim_delete(s_card, flip_anim_exec);
    s_busy = false;
    lv_obj_set_style_transform_scale_x(s_card, 256, 0);
    lv_label_set_text(s_answer, "心里默想一个问题");
    lv_obj_set_style_text_color(s_answer, lv_color_hex(COLOR_TEXT), 0);
    lv_label_set_text(s_hint, "点按屏幕，翻开答案");
}

static void screen_event(lv_event_t *event)
{
    if (lv_event_get_code(event) == LV_EVENT_SCREEN_UNLOADED) answer_book_reset();
}

lv_obj_t *answer_book_build(void)
{
    s_answer_font = ui_font_fixed_20;
    s_answer_font.fallback = &ui_font_detail_20;
    s_screen = lv_obj_create(NULL);
    lv_obj_remove_flag(s_screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_screen, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_screen, screen_event, LV_EVENT_SCREEN_UNLOADED, NULL);

    lv_obj_t *title = make_label(s_screen, "答案之书", COLOR_WARM);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 62);

    s_card = lv_obj_create(s_screen);
    lv_obj_set_size(s_card, 306, 184);
    lv_obj_align(s_card, LV_ALIGN_CENTER, 0, -4);
    lv_obj_set_style_radius(s_card, 22, 0);
    lv_obj_set_style_bg_color(s_card, lv_color_hex(0x17150F), 0);
    lv_obj_set_style_bg_opa(s_card, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(s_card, 1, 0);
    lv_obj_set_style_border_color(s_card, lv_color_hex(0x8C713B), 0);
    lv_obj_set_style_pad_all(s_card, 16, 0);
    lv_obj_set_style_transform_pivot_x(s_card, 153, 0);
    lv_obj_set_style_transform_pivot_y(s_card, 92, 0);
    lv_obj_remove_flag(s_card, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);

    lv_obj_t *mark = make_label(s_card, "DOTII", COLOR_WARM);
    lv_obj_align(mark, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_style_opa(mark, LV_OPA_70, 0);

    s_answer = make_label(s_card, "心里默想一个问题", COLOR_TEXT);
    lv_obj_set_width(s_answer, 258);
    lv_obj_set_style_text_align(s_answer, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_line_space(s_answer, 8, 0);
    lv_label_set_long_mode(s_answer, LV_LABEL_LONG_WRAP);
    lv_obj_align(s_answer, LV_ALIGN_CENTER, 0, 10);

    s_hint = make_label(s_screen, "点按屏幕，翻开答案", COLOR_MUTED);
    lv_obj_align(s_hint, LV_ALIGN_BOTTOM_MID, 0, -64);
    return s_screen;
}
