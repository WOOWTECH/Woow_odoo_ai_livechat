async page => {
  const msgs = page.locator('.o-livechat-root').locator('.o-mail-Message');
  const count = await msgs.count();
  const result = [];
  for (let i = 0; i < count; i++) {
    const text = await msgs.nth(i).textContent();
    result.push(text.substring(0, 150));
  }
  return JSON.stringify({ count, messages: result });
}
