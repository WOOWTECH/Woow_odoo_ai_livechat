async (page) => {
  // Set up notification/error capture
  await page.evaluate(() => {
    window._errors = [];
    window._notifs = [];
    const obs = new MutationObserver(function(muts) {
      muts.forEach(function(m) {
        m.addedNodes.forEach(function(n) {
          if (n.nodeType === 1 && n.querySelector) {
            // Capture notifications
            var notif = n.querySelector('.o_notification_content');
            if (notif) window._notifs.push(notif.textContent);
            if (n.classList && n.classList.contains('o_notification'))
              window._notifs.push(n.textContent);
            // Capture error dialogs
            var dialog = n.querySelector('.modal-body, .o_error_dialog');
            if (dialog) window._errors.push(dialog.textContent);
          }
        });
      });
    });
    obs.observe(document.body, {childList: true, subtree: true});
  });

  const results = [];

  // Test 1: Invalid URL (ftp)
  const urlField = page.locator('[name="ai_api_base_url"] input');
  const origUrl = await urlField.inputValue();
  await urlField.fill('ftp://invalid.com');
  // Try to save
  await page.evaluate(() => {
    var btn = document.querySelector('.o_form_button_save');
    if (btn) btn.click();
  });
  await page.waitForTimeout(2000);
  var errors = await page.evaluate(() => {
    var notifs = [...window._notifs, ...window._errors];
    window._notifs = [];
    window._errors = [];
    return notifs;
  });
  results.push({test: 'Invalid URL (ftp)', errors: errors});

  // Restore and discard
  await page.evaluate(() => {
    var btn = document.querySelector('.o_form_button_cancel');
    if (btn) btn.click();
  });
  await page.waitForTimeout(1000);
  // Handle discard dialog if it appears
  await page.evaluate(() => {
    var btn = document.querySelector('.modal-footer .btn-primary');
    if (btn) btn.click();
  });
  await page.waitForTimeout(1000);

  // Re-click AI tab
  await page.evaluate(() => {
    var tab = document.querySelector('a[name="ai_integration"]');
    if (tab) tab.click();
  });
  await page.waitForTimeout(500);

  // Test 2: Temperature > 2.0
  const tempField = page.locator('[name="ai_temperature"] input');
  await tempField.fill('2.5');
  await page.evaluate(() => {
    var btn = document.querySelector('.o_form_button_save');
    if (btn) btn.click();
  });
  await page.waitForTimeout(2000);
  errors = await page.evaluate(() => {
    var notifs = [...window._notifs, ...window._errors];
    window._notifs = [];
    window._errors = [];
    return notifs;
  });
  results.push({test: 'Temperature > 2.0', errors: errors});

  // Discard
  await page.evaluate(() => {
    var btn = document.querySelector('.o_form_button_cancel');
    if (btn) btn.click();
  });
  await page.waitForTimeout(1000);
  await page.evaluate(() => {
    var btn = document.querySelector('.modal-footer .btn-primary');
    if (btn) btn.click();
  });
  await page.waitForTimeout(1000);

  // Re-click AI tab
  await page.evaluate(() => {
    var tab = document.querySelector('a[name="ai_integration"]');
    if (tab) tab.click();
  });
  await page.waitForTimeout(500);

  // Test 3: Max History = 0
  const histField = page.locator('[name="ai_max_history"] input');
  await histField.fill('0');
  await page.evaluate(() => {
    var btn = document.querySelector('.o_form_button_save');
    if (btn) btn.click();
  });
  await page.waitForTimeout(2000);
  errors = await page.evaluate(() => {
    var notifs = [...window._notifs, ...window._errors];
    window._notifs = [];
    window._errors = [];
    return notifs;
  });
  results.push({test: 'Max History = 0', errors: errors});

  // Discard
  await page.evaluate(() => {
    var btn = document.querySelector('.o_form_button_cancel');
    if (btn) btn.click();
  });
  await page.waitForTimeout(1000);
  await page.evaluate(() => {
    var btn = document.querySelector('.modal-footer .btn-primary');
    if (btn) btn.click();
  });
  await page.waitForTimeout(1000);

  // Re-click AI tab
  await page.evaluate(() => {
    var tab = document.querySelector('a[name="ai_integration"]');
    if (tab) tab.click();
  });
  await page.waitForTimeout(500);

  // Test 4: Clear API Key (required field)
  const keyField = page.locator('[name="ai_api_key"] input');
  await keyField.fill('');
  await page.evaluate(() => {
    var btn = document.querySelector('.o_form_button_save');
    if (btn) btn.click();
  });
  await page.waitForTimeout(2000);
  errors = await page.evaluate(() => {
    var notifs = [...window._notifs, ...window._errors];
    window._notifs = [];
    window._errors = [];
    return notifs;
  });
  results.push({test: 'Empty API Key', errors: errors});

  // Discard
  await page.evaluate(() => {
    var btn = document.querySelector('.o_form_button_cancel');
    if (btn) btn.click();
  });
  await page.waitForTimeout(1000);
  await page.evaluate(() => {
    var btn = document.querySelector('.modal-footer .btn-primary');
    if (btn) btn.click();
  });
  await page.waitForTimeout(1000);

  return results;
}
