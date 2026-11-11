// Token bucket: `rate` per second on average, at most `burst` at once.
// bucket = { tokens, at }: start it full, { tokens: burst, at: Date.now() }.
export function take(bucket, rate, burst) {
  const now = Date.now();
  bucket.tokens = Math.min(burst, bucket.tokens + ((now - bucket.at) / 1000) * rate);
  bucket.at = now;
  if (bucket.tokens < 1) {
    return false;
  }
  bucket.tokens -= 1;
  return true;
}
