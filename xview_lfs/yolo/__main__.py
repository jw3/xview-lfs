from PIL import Image
from tqdm import tqdm
import logging
import configargparse as argparse
import os
import xview_lfs as data
import xview.wv_util as wv
import tempfile
from . import write_yolo_labels
import lfs


def make_temp_dir():
    d = tempfile.mkdtemp(prefix='yolo-')
    os.chmod(d, 0o755)
    return d


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("lfs_url", type=str, help="LFS repo URL")
    parser.add_argument("-r", "--ref", type=str, default='master', help="LFS data ref")
    parser.add_argument("-i", "--images", type=str, help="List of image ids to include; empty for all")
    parser.add_argument("-d", "--dictionary", type=str, help="Path to class dictionary; defaults to xview dict")
    parser.add_argument("-c", "--classes", type=str, help="Class ids from labels to include; empty for all")
    parser.add_argument("-s", "--chip_size", type=int, default=544, help="Training chip size")
    parser.add_argument("-t", "--chip_format", type=str, default='png', help="Training chip format (jpg, png, ...)")
    parser.add_argument("-p", "--prune_empty", action='store_true', help='Prune empty chips')
    parser.add_argument("-w", "--workspace", help="Working directory")

    args = parser.parse_args()

    Image.init()
    if f'.{args.chip_format}' not in Image.EXTENSION.keys():
        raise SystemError(f'invalid chip format, {args.chip_format}')

    if not args.workspace:
        args.workspace = make_temp_dir()

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    boxes = {}
    skip_chips = 0
    images_list = []
    classes_actual = {}

    images = []
    if args.images:
        images = args.images.split(',')
        print('using images: %s' % images)

    print('------------ loading data --------------')
    res = (args.chip_size, args.chip_size)
    lfs_wd, d = data.load_train_data(images, url=args.lfs_url, ref=args.ref, chipsz=res)
    logger.info(f'lfs working directory: {lfs_wd}')

    class_dict = args.dictionary
    if not class_dict:
        labels = wv.get_classes()
    elif lfs.is_uri(class_dict):
        labels = wv.get_classes(lfs.get(class_dict))
    elif os.path.exists(class_dict):
        labels = wv.get_classes(class_dict)
    elif not class_dict.startswith('/') and os.path.exists(os.path.join(lfs_wd, class_dict)):
        labels = wv.get_classes(os.path.join(lfs_wd, class_dict))
    else:
        raise SystemError(f'invalid dictionary path, {class_dict}')

    logger.info(f'class dictionary: {class_dict}')

    if args.classes:
        splits = map(lambda s: int(s), args.classes.split(','))
        for rem in set(labels.keys()) - set(splits):
            labels.pop(rem, None)

    # prepare the working directory
    images_dir = os.path.join(lfs_wd, 'images')
    labels_dir = os.path.join(lfs_wd, 'labels')
    for p in [images_dir, labels_dir]:
        if not os.path.exists(p):
            os.mkdir(p)

    tot_box = 0
    ind_chips = 0

    for iid in tqdm(d.keys()):
        im, box, classes_final = d[iid]

        for _, v in classes_final.items():
            for c in v:
                classes_actual[int(c)] = classes_actual.get(c, 0) + 1

        for idx, image in enumerate(im):
            tf_example = write_yolo_labels(image, box[idx], classes_final[idx], labels)

            if tf_example or not args.prune_empty:
                tot_box += tf_example.count('\n')

                chipid = str(ind_chips).rjust(6, '0')
                writer = open(os.path.join(lfs_wd, f'labels/{chipid}.txt'), "w")
                img_file = os.path.join(lfs_wd, f'images/{chipid}.{args.chip_format}')
                Image.fromarray(image).save(img_file)
                images_list.append(img_file)

                writer.write(tf_example)
                writer.close()

                ind_chips += 1
            else:
                skip_chips += 1

    logging.info("Tot Box: %d" % tot_box)
    logging.info("Chips: %d" % ind_chips)
    logging.info("Skipped Chips: %d" % skip_chips)

    final_classes_map = []
    logging.info("Generating xview.pbtxt")
    with open(os.path.join(args.workspace, 'xview.pbtxt'), 'w') as f:
        idx = 0
        for k, v in classes_actual.items():
            if k in labels:
                idx += 1
                name = labels[k]
                logging.info(' {:>3} {:25}{:>5}'.format(k, name, v))
                f.write('item {{\n  id: {}\n  name: {!r}\n}}\n'.format(idx, name))
                final_classes_map.append(name)
        logging.debug("wrote %s" % f.name)

    logging.info("Generating rewrite_labels.sh")
    with open(os.path.join(args.workspace, 'rewrite_labels.sh'), 'w') as f:
        f.write('''#!/bin/bash\n''')
        for i, c in enumerate(final_classes_map):
            f.write('sed -i "s#{}#{}#g" {}/*\n'.format(c, i, labels_dir))
        os.chmod(f.name, 0o755)
        logging.debug("wrote %s" % f.name)

    with open(os.path.join(args.workspace, 'label_string.txt'), 'w') as f:
        labelstr = ",".join(final_classes_map)
        logging.info("your label string is: {}".format(labelstr))
        f.write(labelstr)
        logging.debug("wrote %s" % f.name)

    logging.info("Generating training_list.txt")
    with open(os.path.join(args.workspace, 'training_list.txt'), 'w') as f:
        f.write('\n'.join(images_list))
        logging.debug("wrote %s" % f.name)
