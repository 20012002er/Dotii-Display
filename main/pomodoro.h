#ifndef POMODORO_H
#define POMODORO_H

#include "app_state.h"
#include "lvgl.h"

lv_obj_t *pomodoro_build(void);
lv_obj_t *pomodoro_content(void);
void pomodoro_apply(const codex_snapshot_t *snapshot);
void pomodoro_tick(void);
void pomodoro_set_finish_callback(void (*callback)(void));
void pomodoro_screen_clicked(void);

#endif