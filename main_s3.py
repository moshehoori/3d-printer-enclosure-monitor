import sys, time
while True:
    try:
        import env_display
    except Exception as e:
        sys.print_exception(e)
        time.sleep(5)
    import machine
    machine.reset()
