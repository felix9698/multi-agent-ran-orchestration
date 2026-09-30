/* Hardware-free harness for the OAI MAC quota API added by the Gate 8 patch.
 *
 * Compile this file against a temporary OAI source copy after applying
 * oai_patches/e2sm_rc_style2_action6_slice_prb.patch.  It deliberately does
 * not start or link nr-softmodem and performs no E2 or RF operation.
 */

#include "LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch_default_policies.h"

#include <assert.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The standalone object retains OAI's lock assertions but not the softmodem
 * process layer that normally owns this symbol. */
void exit_function(const char *file,
                   const char *function,
                   const int line,
                   const char *message,
                   const int is_assert)
{
  fprintf(stderr,
          "OAI assertion in %s:%d (%s): %s [%d]\n",
          file,
          line,
          function,
          message,
          is_assert);
  abort();
}

static bool same_quota(nr_slice_prb_quota_t left, nr_slice_prb_quota_t right)
{
  return left.nssai.sst == right.nssai.sst && left.nssai.sd == right.nssai.sd
         && left.min_ratio == right.min_ratio && left.max_ratio == right.max_ratio
         && left.dedicated_ratio == right.dedicated_ratio;
}

static bool same_entries(const nr_slice_prb_quota_table_t *table,
                         const nr_slice_prb_quota_t *entries,
                         size_t count)
{
  if (table->count != count)
    return false;
  for (size_t i = 0; i < count; ++i) {
    if (!same_quota(table->entries[i], entries[i]))
      return false;
  }
  return true;
}

int main(void)
{
  gNB_MAC_INST mac = {0};
  assert(pthread_mutex_init(&mac.sched_lock, NULL) == 0);
  char reason[128] = {0};

  const nr_slice_prb_quota_t initial[] = {
      {.nssai = {.sst = 1, .sd = 0xffffff}, .min_ratio = 10, .max_ratio = 70, .dedicated_ratio = 5},
      {.nssai = {.sst = 222, .sd = 0x00007b}, .min_ratio = 20, .max_ratio = 80, .dedicated_ratio = 10},
  };
  assert(nr_mac_set_slice_prb_quotas(&mac, initial, 2, reason, sizeof(reason)));
  nr_slice_prb_quota_table_t current = nr_mac_get_slice_prb_quotas(&mac);
  assert(current.generation == 1 && same_entries(&current, initial, 2));

  const nr_slice_prb_quota_t changed[] = {
      {.nssai = {.sst = 222, .sd = 0x00007b}, .min_ratio = 30, .max_ratio = 90, .dedicated_ratio = 15},
  };
  assert(nr_mac_set_slice_prb_quotas(&mac, changed, 1, reason, sizeof(reason)));
  current = nr_mac_get_slice_prb_quotas(&mac);
  nr_slice_prb_quota_table_t previous = nr_mac_get_previous_slice_prb_quotas(&mac);
  assert(current.generation == 2 && same_entries(&current, changed, 1));
  assert(previous.generation == 1 && same_entries(&previous, initial, 2));
  puts("apply:PASS");

  const nr_slice_prb_quota_t duplicate[] = {changed[0], changed[0]};
  assert(!nr_mac_set_slice_prb_quotas(&mac, duplicate, 2, reason, sizeof(reason)));
  current = nr_mac_get_slice_prb_quotas(&mac);
  assert(current.generation == 2 && same_entries(&current, changed, 1));
  puts("malformed:PASS");

  assert(nr_mac_set_slice_prb_quotas(&mac,
                                     previous.entries,
                                     previous.count,
                                     reason,
                                     sizeof(reason)));
  current = nr_mac_get_slice_prb_quotas(&mac);
  assert(current.generation == 3 && same_entries(&current, initial, 2));
  previous = nr_mac_get_previous_slice_prb_quotas(&mac);
  assert(previous.generation == 2 && same_entries(&previous, changed, 1));
  puts("rollback:PASS");

  assert(pthread_mutex_destroy(&mac.sched_lock) == 0);
  return 0;
}
