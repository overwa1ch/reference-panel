#import <AppKit/AppKit.h>
#import <AVFoundation/AVFoundation.h>
#import <ImageIO/ImageIO.h>
#import <PDFKit/PDFKit.h>
#import <QuickLookThumbnailing/QuickLookThumbnailing.h>

static const CGFloat MaxEdge = 1600.0;

static BOOL writePNG(CGImageRef image, NSString *output) {
    NSURL *url = [NSURL fileURLWithPath:output];
    CGImageDestinationRef destination = CGImageDestinationCreateWithURL((__bridge CFURLRef)url, CFSTR("public.png"), 1, NULL);
    if (!destination) return NO;
    CGImageDestinationAddImage(destination, image, NULL);
    BOOL written = CGImageDestinationFinalize(destination);
    CFRelease(destination);
    return written;
}

static BOOL writeJPEG(CGImageRef image, NSString *output) {
    NSURL *url = [NSURL fileURLWithPath:output];
    CGImageDestinationRef destination = CGImageDestinationCreateWithURL((__bridge CFURLRef)url, CFSTR("public.jpeg"), 1, NULL);
    if (!destination) return NO;
    NSDictionary *properties = @{(__bridge NSString *)kCGImageDestinationLossyCompressionQuality: @0.82};
    CGImageDestinationAddImage(destination, image, (__bridge CFDictionaryRef)properties);
    BOOL written = CGImageDestinationFinalize(destination);
    CFRelease(destination);
    return written;
}

// Quick Look adds alpha even to photos and pages; flatten onto white so the
// thumbnail can be a small JPEG without black transparent areas.
static CGImageRef flattened(CGImageRef image) {
    size_t width = CGImageGetWidth(image), height = CGImageGetHeight(image);
    CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef context = CGBitmapContextCreate(NULL, width, height, 8, 0, space, (CGBitmapInfo)kCGImageAlphaNoneSkipLast);
    CGColorSpaceRelease(space);
    if (!context) return NULL;
    CGContextSetRGBFillColor(context, 1, 1, 1, 1);
    CGContextFillRect(context, CGRectMake(0, 0, width, height));
    CGContextDrawImage(context, CGRectMake(0, 0, width, height), image);
    CGImageRef output = CGBitmapContextCreateImage(context);
    CGContextRelease(context);
    return output;
}

// One Quick Look thumbnail (video frame, page, image or document) per file,
// plus the duration of video and audio for the list badge.
static NSDictionary *thumbnail(NSString *kind, NSString *source, NSString *base, CGFloat edge) {
    NSURL *url = [NSURL fileURLWithPath:source];
    NSMutableDictionary *result = [NSMutableDictionary dictionaryWithObject:@"none" forKey:@"status"];
    QLThumbnailGenerationRequest *request = [[QLThumbnailGenerationRequest alloc] initWithFileAtURL:url size:CGSizeMake(edge, edge)
        scale:1.0 representationTypes:QLThumbnailGenerationRequestRepresentationTypeThumbnail];
    QLThumbnailGenerator *generator = QLThumbnailGenerator.sharedGenerator;
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block CGImageRef image = NULL;
    [generator generateBestRepresentationForRequest:request completionHandler:^(QLThumbnailRepresentation *representation, NSError *error) {
        (void)error;
        if (representation.CGImage) image = CGImageRetain(representation.CGImage);
        dispatch_semaphore_signal(done);
    }];
    if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(8 * NSEC_PER_SEC)))) {
        [generator cancelRequest:request];
        result[@"status"] = @"timeout";
    } else if (image) {
        // Only sources that can be transparent keep a PNG thumbnail.
        BOOL jpeg = ![kind isEqualToString:@"alpha"];
        if (jpeg) {
            CGImageRef opaque = flattened(image);
            CGImageRelease(image);
            image = opaque;
        }
        NSString *output = [base stringByAppendingPathExtension:jpeg ? @"jpg" : @"png"];
        if (image && (jpeg ? writeJPEG(image, output) : writePNG(image, output))) {
            [result addEntriesFromDictionary:@{@"status": @"ok", @"file": output, @"mime": jpeg ? @"image/jpeg" : @"image/png",
                                               @"width": @(CGImageGetWidth(image)), @"height": @(CGImageGetHeight(image))}];
        }
        if (image) CGImageRelease(image);
    }
    if ([kind isEqualToString:@"video"] || [kind isEqualToString:@"audio"]) {
        Float64 seconds = CMTimeGetSeconds([AVURLAsset URLAssetWithURL:url options:nil].duration);
        if (isfinite(seconds) && seconds > 0) result[@"duration"] = @(seconds);
    }
    return result;
}

// Decode only to the thumbnail size, in pixels, so DPI tags and huge
// HEIC/TIFF sources neither shrink the preview nor exhaust memory.
static BOOL renderImage(NSString *source, NSString *output) {
    CGImageSourceRef imageSource = CGImageSourceCreateWithURL((__bridge CFURLRef)[NSURL fileURLWithPath:source], NULL);
    if (!imageSource) return NO;
    NSDictionary *options = @{
        (__bridge NSString *)kCGImageSourceCreateThumbnailFromImageAlways: @YES,
        (__bridge NSString *)kCGImageSourceCreateThumbnailWithTransform: @YES,
        (__bridge NSString *)kCGImageSourceShouldCacheImmediately: @NO,
        (__bridge NSString *)kCGImageSourceThumbnailMaxPixelSize: @(MaxEdge),
    };
    CGImageRef thumbnail = CGImageSourceCreateThumbnailAtIndex(imageSource, 0, (__bridge CFDictionaryRef)options);
    CFRelease(imageSource);
    if (!thumbnail) return NO;
    BOOL written = writePNG(thumbnail, output);
    CGImageRelease(thumbnail);
    return written;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        // thumbs <max-edge> <output-dir> <kind\tpath>...
        if (argc >= 4 && strcmp(argv[1], "thumbs") == 0) {
            CGFloat edge = MAX(64, MIN(1024, [@(argv[2]) doubleValue]));
            NSString *directory = @(argv[3]);
            NSMutableArray *items = [NSMutableArray array];
            for (int index = 4; index < argc; index++) {
                NSString *entry = @(argv[index]);
                NSRange tab = [entry rangeOfString:@"\t"];
                if (tab.location == NSNotFound) { [items addObject:@{@"status": @"invalid"}]; continue; }
                NSString *kind = [entry substringToIndex:tab.location], *path = [entry substringFromIndex:NSMaxRange(tab)];
                NSString *base = [directory stringByAppendingPathComponent:[NSString stringWithFormat:@"%d", index - 4]];
                @autoreleasepool { [items addObject:thumbnail(kind, path, base, edge)]; }
            }
            NSData *json = [NSJSONSerialization dataWithJSONObject:items options:0 error:nil];
            fwrite(json.bytes, 1, json.length, stdout);
            fputc('\n', stdout);
            return 0;
        }
        if (argc != 5) return 2;
        NSString *kind = @(argv[1]), *source = @(argv[2]), *output = @(argv[3]);
        NSInteger pageNumber = [@(argv[4]) integerValue];
        NSUInteger pages = 1;
        if ([kind isEqualToString:@"pdf"]) {
            PDFDocument *document = [[PDFDocument alloc] initWithURL:[NSURL fileURLWithPath:source]];
            if (!document) return 3;
            if (document.isLocked) return 7;
            pages = document.pageCount;
            if (pages == 0) return 3;
            // A document can shrink between page turns; show its last page.
            pageNumber = MAX(1, MIN(pageNumber, (NSInteger)pages));
            PDFPage *page = [document pageAtIndex:(NSUInteger)pageNumber - 1];
            NSImage *image = [page thumbnailOfSize:NSMakeSize(MaxEdge, MaxEdge) forBox:kPDFDisplayBoxCropBox];
            CGImageRef rendered = [image CGImageForProposedRect:NULL context:nil hints:nil];
            if (!rendered || !writePNG(rendered, output)) return 5;
        } else if ([kind isEqualToString:@"image"]) {
            pageNumber = 1;
            if (!renderImage(source, output)) return 4;
        } else {
            return 2;
        }
        NSData *json = [NSJSONSerialization dataWithJSONObject:@{@"page": @(pageNumber), @"pageCount": @(pages)} options:0 error:nil];
        fwrite(json.bytes, 1, json.length, stdout);
        fputc('\n', stdout);
    }
    return 0;
}
