async page => {
  const input = page.locator('.o-livechat-root').locator('textarea').first();
  await input.fill("AI重新啟用後的測試訊息");
  await input.press('Enter');

  const beforeCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(500);
    const currentCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
    if (currentCount > beforeCount) {
      const lastMsg = page.locator('.o-livechat-root').locator('.o-mail-Message').last();
      const text = await lastMsg.textContent();
      return JSON.stringify({ success: true, reply: text.substring(0, 200), msgCount: currentCount });
    }
  }
  return JSON.stringify({ success: false, reply: "TIMEOUT" });
}
