"""
Ranker daemon — stub.

Next phase: every 5 minutes, fetch candidates from post_candidates +
engagement_metrics, call scoring.compute_scores(), write top 200 rows
to feed_cache and global_feed_cache.
"""
import time

if __name__ == "__main__":
    while True:
        time.sleep(300)
