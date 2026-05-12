async page => {
  await page.waitForTimeout(3000);
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  const input = page.locator('.o-livechat-root').locator('textarea').first();
  await input.fill("你好，請問你們的營業時間是幾點到幾點？");
  await input.press('Enter');

  const beforeCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(500);
    const currentCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
    if (currentCount > beforeCount) {
      const lastMsg = page.locator('.o-livechat-root').locator('.o-mail-Message').last();
      const text = await lastMsg.textContent();
      return JSON.stringify({ reply: text.substring(0, 300), note: "Should be in English despite Chinese question" });
    }
  }
  return JSON.stringify({ reply: "TIMEOUT" });
}
