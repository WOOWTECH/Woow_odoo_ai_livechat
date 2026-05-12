async page => {
  await page.waitForTimeout(3000);
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  const input = page.locator('.o-livechat-root').locator('textarea').first();
  await input.fill("測試錯誤處理");
  await input.press('Enter');

  // Wait for error message (should come after retries fail)
  const beforeCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(500);
    const currentCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
    if (currentCount > beforeCount) {
      const lastMsg = page.locator('.o-livechat-root').locator('.o-mail-Message').last();
      const text = await lastMsg.textContent();
      return JSON.stringify({ success: true, reply: text.substring(0, 300), msgCount: currentCount });
    }
  }
  return JSON.stringify({ success: false, reply: "TIMEOUT - no error message received" });
}
