async page => {
  // Click chat button
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  const input = page.locator('.o-livechat-root').locator('textarea').first();
  await input.fill(page._testMessage || "你好，這是一條測試訊息");
  await input.press('Enter');

  // Wait for AI reply
  const beforeCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
  for (let i = 0; i < 60; i++) {
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
