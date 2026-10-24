#import <AppKit/AppKit.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void emit(NSDictionary *value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:0 error:nil];
    fwrite(data.bytes, 1, data.length, stdout);
    fputc('\n', stdout);
    fflush(stdout);
}

@interface ReferenceHelper : NSObject <NSApplicationDelegate>
@property(strong) NSOpenPanel *panel;
@property BOOL selecting;
- (void)handleCommand:(NSString *)command;
@end

@implementation ReferenceHelper
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    self.panel = NSOpenPanel.openPanel;
    self.panel.title = @"添加参考";
    self.panel.message = @"选择参考文件，支持多选";
    self.panel.prompt = @"添加";
    self.panel.canChooseFiles = YES;
    self.panel.canChooseDirectories = NO;
    self.panel.allowsMultipleSelection = YES;
    self.panel.canCreateDirectories = NO;
    self.panel.collectionBehavior = NSWindowCollectionBehaviorMoveToActiveSpace |
                                    NSWindowCollectionBehaviorFullScreenAuxiliary;
    emit(@{@"status": @"ready", @"source": @"NSOpenPanel"});
}

- (void)handleCommand:(NSString *)command {
    if ([command isEqualToString:@"ping"]) {
        emit(@{@"status": @"ok", @"eventLoop": @"running"});
        return;
    }
    if ([command isEqualToString:@"cancel"]) {
        if (self.selecting) [self.panel cancel:nil];
        return;
    }
    if ([command isEqualToString:@"drag-paths"]) {
        NSPasteboard *board = [NSPasteboard pasteboardWithName:NSPasteboardNameDrag];
        NSInteger change = board.changeCount;
        NSArray *urls = [board readObjectsForClasses:@[NSURL.class]
                                            options:@{NSPasteboardURLReadingFileURLsOnlyKey: @YES}];
        NSMutableArray *paths = [NSMutableArray array];
        for (NSURL *url in urls) {
            if (url.isFileURL && url.path) [paths addObject:url.path];
        }
        BOOL stable = change == board.changeCount;
        emit(@{@"status": stable ? @"captured" : @"unavailable",
               @"source": @"NSPasteboardNameDrag", @"changeCount": @(change),
               @"paths": stable ? paths : @[]});
        return;
    }
    if (![command isEqualToString:@"select"]) return;
    if (self.selecting) {
        emit(@{@"status": @"error", @"message": @"文件选择器已打开"});
        return;
    }
    self.selecting = YES;
    [NSApp unhide:nil];
    [NSApp activateIgnoringOtherApps:YES];
    // Keep the AppKit event loop and main queue free while the user browses.
    [self.panel beginWithCompletionHandler:^(NSModalResponse response) {
        NSMutableArray *paths = [NSMutableArray array];
        if (response == NSModalResponseOK) {
            for (NSURL *url in self.panel.URLs) {
                if (url.isFileURL && url.path) [paths addObject:url.path];
            }
        }
        self.selecting = NO;
        [self.panel orderOut:nil];
        [NSApp hide:nil];
        emit(@{@"status": response == NSModalResponseOK ? @"selected" : @"cancelled",
               @"source": @"NSOpenPanel", @"paths": paths});
    }];
}
@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication;
        [app setActivationPolicy:NSApplicationActivationPolicyAccessory];
        ReferenceHelper *helper = [ReferenceHelper new];
        app.delegate = helper;
        // Blocking pipe reads belong off the UI thread. EOF closes this child.
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            char *line = NULL;
            size_t capacity = 0;
            while (getline(&line, &capacity, stdin) != -1) {
                @autoreleasepool {
                    NSString *command = [[NSString alloc] initWithUTF8String:line];
                    command = [command stringByTrimmingCharactersInSet:NSCharacterSet.newlineCharacterSet];
                    dispatch_async(dispatch_get_main_queue(), ^{ [helper handleCommand:command]; });
                }
            }
            free(line);
            dispatch_async(dispatch_get_main_queue(), ^{ [NSApp terminate:nil]; });
        });
        [app run];
    }
    return 0;
}
