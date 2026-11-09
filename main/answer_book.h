#ifndef ANSWER_BOOK_H
#define ANSWER_BOOK_H

#include <stddef.h>

#include "lvgl.h"

lv_obj_t *answer_book_build(void);
void answer_book_activate(void);

size_t answer_book_answer_count(void);
const char *answer_book_answer_at(size_t index);

#endif
