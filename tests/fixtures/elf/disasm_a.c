/* One of two files that each define a static function called scale, for the
 * test that a name matching two functions is refused, not resolved to one. */
static int scale(int x) { return x * 3 + 1; }
int scale_a(int x) { return scale(x); }
