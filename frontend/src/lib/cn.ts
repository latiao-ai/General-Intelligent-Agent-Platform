/** 拼类名。只做过滤 + 拼接，不做任何去重或排序——保持可预测。 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
