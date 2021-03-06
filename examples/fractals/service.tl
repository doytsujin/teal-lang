/**
 * service.tl
 *
 * Generates a random selection of fractals, spinning up a new thread for each
 * fractal.
 *
 * Running this locally will be slow, as the execution time is bound by your
 * CPU. Running in AWS is not - each thread gets its own Lambda.
 *
**/

// The Python functions that to do the heavy lifting.
import(random_fractals,      :python src.draw, 1);
import(save_fractal_to_file, :python src.draw, 3);
import(upload_to_bucket,     :python src.store, 1);


/**
 * Teal doesn't have a standard library yet, so we have to implement
 * functional-style mapping here. This won't be necessary in future versions.
 *
 * map(f, l) takes a function, f, and a list l, and returns a new list with the
 * results of applying f to every element in l.
 *
 * To make it fast, we implement a "tail-recursive" version, so that the Teal
 * compiler can generate optimal code. https://en.wikipedia.org/wiki/Tail_call
**/

// Tail-recursive version of `map`
fn map_tr(func, items, acc) {
  if nullp(items) {
    acc
  }
  else {
    map_tr(func, rest(items), append(acc, func(first(items))))
  }
}

// Now `map` is just a wrapper around the tail recursive version.
//
// In Teal, `null` is equivalent to the empty list, and we don't have the
// familiar '[]' syntax for lists yet!
fn map(func, items) {
  map_tr(func, items, null)
}


fn wait(item) {
  await item
}

/**
 * `map_wait` is a version of `map` which first maps `func` over some items, and
 * then does `await` on each element.
 *
 * Use `map_wait` when `func` returns a Future.
**/
fn map_wait(func, items) {
  map(wait, map(func, items))
}


/**
 * `build_fractal` renders a fractal and uploads it to S3.
**/
fn build_fractal(fractal_spec) {
  // fractal_spec :: [fractal_type, fractal_size]
  print(fractal_spec);
  result = save_fractal_to_file(nth(fractal_spec, 0), nth(fractal_spec, 1), "/tmp");
  upload_to_bucket(result)
}


// Python functions can't be called with `async`, so we have to wrap them in a
// Teal function. This isn't ideal, and should be fixed in future versions.
fn build_fractal_async(fractal_spec) {
  async build_fractal(fractal_spec)
}


/**
 * Entrypoint. Get some random fractal specs, and then render them in parallel.
**/
fn main() {
  fractal_specs = random_fractals(4);
  results = map_wait(build_fractal_async, fractal_specs);
  print("Done");
  results
}
