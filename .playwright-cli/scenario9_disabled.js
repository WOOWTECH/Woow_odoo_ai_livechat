async page => {
  await page.waitForTimeout(3000);
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  const input = page.locator('.o-livechat-root').locator('textarea').first();
  await input.fill("AI已停用的測試訊息");
  await input.press('Enter');

  // Wait 10 seconds — no AI reply should come
  await page.waitForTimeout(10000);

  const msgCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
  return JSON.stringify({ msgCount, note: "Should be 2 (welcome + user msg, no AI)" });
}
