"""Read a dark line on a raster plot with manually verified frame and scale."""

import math
from statistics import median


def read_line(page, config):
    import cv2
    import numpy as np

    required = {"corners_pt", "x_ticks", "y_ticks", "sample_x", "max_gray"}
    if not isinstance(config, dict) or not required <= config.keys():
        raise ValueError("raster settings require corners_pt, x_ticks, y_ticks, sample_x and max_gray")
    if type(config["max_gray"]) is not int or not 1 <= config["max_gray"] <= 255:
        raise ValueError("max_gray must be an integer in 1..255")
    if not isinstance(config["sample_x"], list) or not config["sample_x"]:
        raise ValueError("sample_x must contain at least one value")
    corners = np.asarray(config["corners_pt"], dtype=np.float32)
    if corners.shape != (4, 2) or not np.isfinite(corners).all():
        raise ValueError("four finite plot corners are required")
    if not cv2.isContourConvex(corners) or cv2.contourArea(corners, oriented=True) <= 0:
        raise ValueError("corners must follow clockwise order from top left")
    if (corners.min() < 0 or corners[:, 0].max() > page.width or corners[:, 1].max() > page.height):
        raise ValueError("raster frame must fit on the PDF page")

    image = page.to_image(resolution=288).original.convert("L")
    pixels_per_pt = np.array([image.width / page.width, image.height / page.height])
    source = (corners * pixels_per_pt).astype(np.float32)
    width = round(max(math.dist(source[0], source[1]), math.dist(source[3], source[2])))
    height = round(max(math.dist(source[0], source[3]), math.dist(source[1], source[2])))
    if min(width, height) < 20:
        raise ValueError("raster frame is too small to calibrate")
    target = np.float32([(0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1)])
    transform = cv2.getPerspectiveTransform(source, target)
    inverse = cv2.getPerspectiveTransform(target, source)
    gray = cv2.warpPerspective(np.asarray(image), transform, (width, height))

    def project(matrix, points):
        return cv2.perspectiveTransform(np.array([points], dtype=np.float32), matrix)[0]

    axes = {}
    for coordinate, axis in enumerate(("x", "y")):
        landmarks = np.asarray(config[f"{axis}_ticks"], dtype=np.float64)
        if landmarks.ndim != 2 or landmarks.shape[1] != 3 or len(landmarks) < 2 or not np.isfinite(landmarks).all():
            raise ValueError(f"{axis}: at least two finite scale landmarks required")
        positions = project(transform, landmarks[:, :2] * pixels_per_pt)[:, coordinate]
        values = landmarks[:, 2]
        if np.ptp(positions) < 1 or np.ptp(values) == 0:
            raise ValueError(f"{axis}: scale landmarks must be distinct")
        slope, intercept = np.polyfit(positions, values, 1)
        residual = float(np.max(np.abs(positions * slope + intercept - values)))
        if residual > abs(slope) * 2:
            raise ValueError(f"{axis}: landmarks disagree with a linear scale")
        axes[axis] = {"scale": "linear", "label_source": "manual", "slope_per_pixel": slope,
                      "intercept": intercept, "max_tick_residual": residual,
                      "ticks": [{"source_pt": [x, y], "position_px": float(position), "value": value}
                                for (x, y, value), position in zip(landmarks.tolist(), positions)]}

    def source_box(points):
        area = project(inverse, points) / pixels_per_pt
        left, top = np.min(area, axis=0)
        right, bottom = np.max(area, axis=0)
        return [float(left), float(top), float(right), float(bottom)]

    observations, issues = [], []
    for value in config["sample_x"]:
        if not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError("sample values must be finite")
        x = round((value - axes["x"]["intercept"]) / axes["x"]["slope_per_pixel"])
        if not 4 <= x < width - 4:
            issues.append({"code": "sample_outside_frame", "sample_x": value,
                           "message": f"x={value}: sample falls outside the data frame"})
            continue
        rows = np.flatnonzero(np.count_nonzero(gray[:, x - 2:x + 3] < config["max_gray"], axis=1) >= 3)
        rows = rows[(rows >= 4) & (rows < height - 4)]
        runs = np.split(rows, np.where(np.diff(rows) > 1)[0] + 1)
        runs = [run for run in runs if len(run) >= 3]
        if len(runs) != 1:
            left, right = x - 3, x + 3
            issues.append({"code": "trace_not_unique", "sample_x": value,
                           "message": f"x={value}: expected one dark trace, found {len(runs)}",
                           "box_pt": source_box([(left, 0), (right, 0),
                                                 (right, height - 1), (left, height - 1)])})
            continue
        y = median(runs[0].tolist())
        center = project(inverse, [(x, y)])[0] / pixels_per_pt
        box = source_box([(x - 3, y - 3), (x + 3, y - 3), (x + 3, y + 3), (x - 3, y + 3)])
        observations.append({"x": value, "y": float(axes["y"]["slope_per_pixel"] * y + axes["y"]["intercept"]),
                             "position_pt": center.tolist(), "box_pt": box})
    return observations, axes, {
        "corners_pt": corners.tolist(),
        "rectified_to_pdf_pt": (np.diag([1 / pixels_per_pt[0], 1 / pixels_per_pt[1], 1]) @ inverse).tolist(),
        "dpi": 288, "opencv_version": cv2.__version__,
    }, issues
