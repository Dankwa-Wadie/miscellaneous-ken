#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

@interface Studio : NSObject <NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate>
@property NSWindow *window;
@property WKWebView *web;
@property NSStatusItem *item;
@property NSInteger attempts;
@end
@implementation Studio
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSMenu *main = [NSMenu new];
    NSMenuItem *root = [NSMenuItem new]; [main addItem:root];
    root.submenu = [NSMenu new];
    [root.submenu addItemWithTitle:@"About Miscellaneous Ken" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [root.submenu addItem:NSMenuItem.separatorItem];
    [root.submenu addItemWithTitle:@"Quit Miscellaneous Ken" action:@selector(terminate:) keyEquivalent:@"q"];
    NSMenuItem *edit = [[NSMenuItem alloc] initWithTitle:@"Edit" action:nil keyEquivalent:@""];
    edit.submenu = [[NSMenu alloc] initWithTitle:@"Edit"];
    for (NSArray *entry in @[@[@"Undo",@"undo:",@"z"],@[@"Cut",@"cut:",@"x"],@[@"Copy",@"copy:",@"c"],@[@"Paste",@"paste:",@"v"],@[@"Select All",@"selectAll:",@"a"]]) {
        [edit.submenu addItemWithTitle:entry[0] action:NSSelectorFromString(entry[1]) keyEquivalent:entry[2]];
    }
    [main addItem:edit];
    NSMenuItem *view = [[NSMenuItem alloc] initWithTitle:@"View" action:nil keyEquivalent:@""];
    view.submenu = [[NSMenu alloc] initWithTitle:@"View"];
    [[view.submenu addItemWithTitle:@"Reload Studio" action:@selector(reloadStudio) keyEquivalent:@"r"] setTarget:self];
    [main addItem:view]; NSApp.mainMenu = main;
    WKWebViewConfiguration *config = [WKWebViewConfiguration new];
    config.websiteDataStore = WKWebsiteDataStore.nonPersistentDataStore;
    self.web = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:config];
    self.web.navigationDelegate = self; self.web.UIDelegate = self;
    self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0,0,1160,800) styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskMiniaturizable|NSWindowStyleMaskResizable backing:NSBackingStoreBuffered defer:NO];
    self.window.title = @"Miscellaneous Ken"; self.window.minSize = NSMakeSize(760,580);
    self.window.contentView = self.web; self.window.releasedWhenClosed = NO; [self.window center];
    self.item = [NSStatusBar.systemStatusBar statusItemWithLength:NSVariableStatusItemLength];
    self.item.button.title = @"K.";
    NSMenu *menu = [NSMenu new];
    [[menu addItemWithTitle:@"Open Studio" action:@selector(show) keyEquivalent:@""] setTarget:self];
    [[menu addItemWithTitle:@"Automation settings" action:@selector(showAutomation) keyEquivalent:@""] setTarget:self];
    [menu addItem:NSMenuItem.separatorItem];
    NSMenuItem *note = [menu addItemWithTitle:@"Pipeline continues when this app closes" action:nil keyEquivalent:@""]; note.enabled = NO;
    [menu addItemWithTitle:@"Quit Studio" action:@selector(terminate:) keyEquivalent:@""];
    self.item.menu = menu; [self show];
    [self.web loadHTMLString:@"<html><body style='background:#101114;color:#ecedf1;font:18px -apple-system;padding:60px'><h1>Miscellaneous Ken</h1><p>Connecting to your studio…</p></body></html>" baseURL:nil];
    NSTask *task = [NSTask new]; task.executableURL = [NSURL fileURLWithPath:@"/bin/launchctl"];
    task.arguments = @[@"kickstart",[NSString stringWithFormat:@"gui/%u/com.miscellaneousken.studio",getuid()]];
    [task launchAndReturnError:nil]; [self connect];
}
- (void)connect {
    NSURL *url = [NSURL URLWithString:@"http://127.0.0.1:8766/api/status"];
    [[[NSURLSession sharedSession] dataTaskWithURL:url completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if ([(NSHTTPURLResponse *)response statusCode] == 200) {
                [self.web loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:@"http://127.0.0.1:8766/"]]];
            } else if (self.attempts++ < 30) {
                dispatch_after(dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC),dispatch_get_main_queue(),^{[self connect];});
            } else {
                NSAlert *alert = [NSAlert new]; alert.messageText = @"The background service could not start";
                alert.informativeText = @"Reinstall the local service with deploy/install-studio.py, then reopen the app. Your saved videos are unchanged.";
                [alert runModal];
            }
        });
    }] resume];
}
- (void)show { [self.window makeKeyAndOrderFront:nil]; [NSApp activateIgnoringOtherApps:YES]; }
- (void)reloadStudio { [self.web reloadFromOrigin]; }
- (void)showAutomation { [self show]; [self.web evaluateJavaScript:@"document.querySelector('[data-page=automation]').click()" completionHandler:nil]; }
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender { return NO; }
- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag { [self show]; return YES; }
- (void)webView:(WKWebView *)webView decidePolicyForNavigationAction:(WKNavigationAction *)action decisionHandler:(void (^)(WKNavigationActionPolicy))handler {
    NSURL *url = action.request.URL;
    if ([url.scheme isEqualToString:@"about"]) { handler(WKNavigationActionPolicyAllow); return; }
    if ([url.scheme isEqualToString:@"http"] && [url.host isEqualToString:@"127.0.0.1"] && url.port.intValue == 8766) {
        handler(action.shouldPerformDownload ? WKNavigationActionPolicyDownload : WKNavigationActionPolicyAllow);
    } else {
        if ([@[@"http",@"https"] containsObject:url.scheme]) [NSWorkspace.sharedWorkspace openURL:url];
        handler(WKNavigationActionPolicyCancel);
    }
}
- (WKWebView *)webView:(WKWebView *)webView createWebViewWithConfiguration:(WKWebViewConfiguration *)config forNavigationAction:(WKNavigationAction *)action windowFeatures:(WKWindowFeatures *)features {
    if ([action.request.URL.scheme isEqualToString:@"https"]) [NSWorkspace.sharedWorkspace openURL:action.request.URL]; return nil;
}
- (void)webView:(WKWebView *)webView runJavaScriptConfirmPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(BOOL))handler {
    NSAlert *alert = [NSAlert new]; alert.messageText = message;
    [alert addButtonWithTitle:@"Continue"]; [alert addButtonWithTitle:@"Cancel"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){handler(response == NSAlertFirstButtonReturn);}];
}
- (void)webView:(WKWebView *)webView runOpenPanelWithParameters:(WKOpenPanelParameters *)params initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(NSArray<NSURL *> *))handler {
    NSOpenPanel *panel = [NSOpenPanel openPanel]; panel.allowsMultipleSelection = NO; panel.canChooseDirectories = NO;
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){handler(response == NSModalResponseOK ? panel.URLs : nil);}];
}
- (void)webView:(WKWebView *)webView navigationAction:(WKNavigationAction *)action didBecomeDownload:(WKDownload *)download { download.delegate = self; }
- (void)download:(WKDownload *)download decideDestinationUsingResponse:(NSURLResponse *)response suggestedFilename:(NSString *)filename completionHandler:(void (^)(NSURL *))handler {
    NSSavePanel *panel = [NSSavePanel savePanel]; panel.nameFieldStringValue = [filename hasSuffix:@".mp4"] ? filename : @"Miscellaneous Ken.mp4";
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){handler(response == NSModalResponseOK ? panel.URL : nil);}];
}
@end
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication; Studio *delegate = [Studio new];
        app.delegate = delegate; [app setActivationPolicy:NSApplicationActivationPolicyRegular]; [app run];
    }
    return 0;
}
